"""
Woogent — Gemini AI Shopping Demo

Shows Gemini using the UCP API to browse and purchase from a WooCommerce store.
The full tool-call chain is visible so judges can watch every API call in real time.

Ports:
  API  → http://localhost:8000
  Demo → http://localhost:7860
"""
import os
import json
import httpx


def _format_assistant_text(text: str) -> str:
    """Normalize Gemini output for cleaner chat rendering."""
    if not text:
        return text

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned: list[str] = []
    prev_was_bullet = False

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        # Normalize common bullet symbols to markdown bullets.
        if stripped.startswith(("• ", "◦ ", "· ", "* ")):
            stripped = "- " + stripped[2:].lstrip()

        is_bullet = stripped.startswith("- ")

        # Add breathing room before a bullet list when transitioning from prose.
        if is_bullet and cleaned and cleaned[-1] and not prev_was_bullet:
            cleaned.append("")

        cleaned.append(stripped)
        prev_was_bullet = is_bullet

    # Collapse excessive blank lines.
    out: list[str] = []
    blank_count = 0
    for line in cleaned:
        if line == "":
            blank_count += 1
            if blank_count <= 1:
                out.append(line)
        else:
            blank_count = 0
            out.append(line)

    return "\n".join(out).strip()


def _to_json_safe(obj):
    """Convert protobuf/RepeatedComposite to JSON-serializable Python."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(v) for v in obj]
    # Dict-like (MapComposite is MutableMapping, not dict) — must check BEFORE __iter__
    # because MutableMapping.__iter__ yields keys, not items
    if hasattr(obj, "items") and callable(getattr(obj, "items")):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    # Protobuf RepeatedComposite / list-like — no .items(), has __iter__
    if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
        try:
            return [_to_json_safe(v) for v in obj]
        except TypeError:
            pass
    # Protobuf Struct (has .fields) — fallback
    if hasattr(obj, "fields"):
        return {k: _to_json_safe(v) for k, v in obj.fields.items()}
    # Protobuf Value wrappers (string_value, number_value, etc.)
    for attr in ("string_value", "number_value", "bool_value"):
        if hasattr(obj, attr):
            return _to_json_safe(getattr(obj, attr))
    if hasattr(obj, "struct_value") and obj.struct_value:
        return _to_json_safe(dict(obj.struct_value.fields))
    if hasattr(obj, "list_value") and obj.list_value:
        return _to_json_safe(list(obj.list_value.values))
    return str(obj)
import gradio as gr
import gradio.networking as _gr_net
import gradio_client.utils as _gcu
_gr_net.url_ok = lambda url: True  # bypass macOS localhost connectivity check

# gradio_client doesn't handle boolean JSON Schemas (true/false) — patch it
_orig_schema = _gcu._json_schema_to_python_type
def _safe_schema(schema, defs=None):
    if isinstance(schema, bool):
        return "Any"
    return _orig_schema(schema, defs)
_gcu._json_schema_to_python_type = _safe_schema

import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000")
API_PUBLIC_URL = os.getenv("API_PUBLIC_URL", API_BASE)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "gemini-demo-client")
OAUTH_CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", "gemini-demo-secret")

genai.configure(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# UCP API client helpers
# ---------------------------------------------------------------------------

def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": _uuid(), "Request-Id": _uuid(), "UCP-Agent": 'profile="https://gemini.google.com/agent"'}


def _uuid() -> str:
    import uuid
    return str(uuid.uuid4())


def get_token() -> str:
    """Obtain a Bearer token via client_credentials-style shortcut for demo."""
    import base64
    import secrets as s
    # Use the demo auth flow: create a code directly via the API's test helper
    # For the demo we call the token endpoint with a pre-issued code
    # In a real flow the user would go through /oauth2/authorize in their browser
    creds = base64.b64encode(f"{OAUTH_CLIENT_ID}:{OAUTH_CLIENT_SECRET}".encode()).decode()
    # Since this is a demo, we hit a special /demo/token shortcut endpoint
    resp = httpx.post(
        f"{API_BASE}/demo/token",
        headers={"Authorization": f"Basic {creds}"},
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.json()["access_token"]
    return ""


# ---------------------------------------------------------------------------
# Tool definitions for Gemini function calling
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "discover_store",
        "description": "Fetch the UCP capability manifest to discover what this store supports.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_products",
        "description": "Search the WooCommerce product catalog. Use this to find products matching user requests. Call repeatedly with increasing offset to paginate through all results.",
        "parameters": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Keyword search term (e.g. 'hoodie', 'blue shirt'). Omit to browse all products."},
                "max_price": {"type": "number", "description": "Maximum price in USD"},
                "limit": {"type": "integer", "description": "Number of results per page (default 20, max 100)"},
                "offset": {"type": "integer", "description": "Number of results to skip for pagination (default 0)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_product",
        "description": "Get full details for a specific product by ID.",
        "parameters": {
            "type": "object",
            "properties": {"product_id": {"type": "string", "description": "WooCommerce product ID"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "create_checkout_session",
        "description": "Start a UCP checkout session with one or more items.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "List of items to purchase",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "string"},
                            "product_title": {"type": "string"},
                            "quantity": {"type": "integer"},
                            "price_micros": {"type": "integer", "description": "Price in micros (e.g. $19.99 = 19990000)"},
                        },
                        "required": ["product_id", "product_title", "quantity", "price_micros"],
                    },
                }
            },
            "required": ["items"],
        },
    },
    {
        "name": "create_checkout_and_complete",
        "description": "Create a checkout session, set the address, and place the order in one call. Use this when the user provides their address. No session_id or shipping selection needed — just items and address.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "List of items to purchase (product_id, product_title, quantity, price_micros)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "string"},
                            "product_title": {"type": "string"},
                            "quantity": {"type": "integer"},
                            "price_micros": {"type": "integer"},
                        },
                        "required": ["product_id", "product_title", "quantity", "price_micros"],
                    },
                },
                "street": {"type": "string"},
                "city": {"type": "string"},
                "state": {"type": "string", "description": "2-letter state code, e.g. IL"},
                "postal_code": {"type": "string"},
                "country": {"type": "string", "description": "ISO country code (e.g. US). Omit for US."},
                "email": {"type": "string", "description": "Buyer's email address for order tracking."},
            },
            "required": ["items", "street", "city", "state", "postal_code", "email"],
        },
    },
    {
        "name": "get_session",
        "description": "Get the current state of a checkout session.",
        "parameters": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "get_order_status",
        "description": "Look up an order by order ID or buyer email. Use this when the user wants to track or check the status of an order. Prefer email when the user provided it during checkout — it's more reliable than order_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Order ID (e.g. '136'). Use if known."},
                "email": {"type": "string", "description": "Buyer's email address. Returns all orders for that email. Prefer this when available."},
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(name: str, args: dict, token: str) -> str:
    headers = _auth_headers(token)
    try:
        if name == "discover_store":
            r = httpx.get(f"{API_BASE}/.well-known/ucp", timeout=10)
            return json.dumps(r.json(), indent=2)

        elif name == "search_products":
            params = {k: v for k, v in args.items() if v is not None}
            if "limit" not in params:
                params["limit"] = 20

            def _search(p: dict) -> dict:
                resp = httpx.get(f"{API_BASE}/ucp/products", params=p, timeout=10)
                data = resp.json()
                data["_status_code"] = resp.status_code
                data["_params_used"] = dict(p)
                return data

            first = _search(params)
            if first.get("_status_code") != 200:
                return json.dumps({k: v for k, v in first.items() if not str(k).startswith("_")}, indent=2)

            if first.get("products"):
                return json.dumps({k: v for k, v in first.items() if not str(k).startswith("_")}, indent=2)

            # Fallback 1: drop max_price (too restrictive) but keep user intent query/category.
            fallback_attempts = []
            if "max_price" in params:
                relaxed = {k: v for k, v in params.items() if k != "max_price"}
                second = _search(relaxed)
                fallback_attempts.append(second)
                if second.get("_status_code") == 200 and second.get("products"):
                    clean = {k: v for k, v in second.items() if not str(k).startswith("_")}
                    clean["_fallback"] = {
                        "strategy": "dropped_max_price",
                        "initial_params": first["_params_used"],
                        "resolved_with_params": second["_params_used"],
                    }
                    return json.dumps(clean, indent=2)

            # Fallback 2: if query had multiple words, retry with just the last meaningful word.
            q_val = params.get("q") or params.get("query", "")
            if q_val and len(q_val.split()) > 1:
                # Strip common filler words and use the last remaining token
                stopwords = {"a", "an", "the", "i", "me", "can", "buy", "get", "want",
                             "some", "please", "find", "show", "need", "looking", "for"}
                words = [w for w in q_val.lower().split() if w not in stopwords]
                if words:
                    keyword_only = {k: v for k, v in params.items() if k not in ("q", "query")}
                    keyword_only["q"] = words[-1]
                    third = _search(keyword_only)
                    fallback_attempts.append(third)
                    if third.get("_status_code") == 200 and third.get("products"):
                        clean = {k: v for k, v in third.items() if not str(k).startswith("_")}
                        clean["_fallback"] = {
                            "strategy": "extracted_keyword",
                            "initial_params": first["_params_used"],
                            "resolved_with_params": third["_params_used"],
                        }
                        return json.dumps(clean, indent=2)

            # Return the original empty result with fallback diagnostics for the assistant.
            clean_first = {k: v for k, v in first.items() if not str(k).startswith("_")}
            clean_first["_fallback"] = {
                "strategy": "no_results_after_fallbacks",
                "initial_params": first["_params_used"],
                "attempted_params": [a["_params_used"] for a in fallback_attempts],
            }
            return json.dumps(clean_first, indent=2)

        elif name == "get_product":
            r = httpx.get(f"{API_BASE}/ucp/products/{args['product_id']}", timeout=10)
            return json.dumps(r.json(), indent=2)

        elif name == "create_checkout_session":
            items = args["items"]

            # Auto-correct: if any product ID is wrong (422 PRODUCT_NOT_FOUND),
            # re-search by title to get the real ID before giving up.
            def _build_body(item_list):
                return {
                    "line_items": [
                        {
                            "item": {"id": i["product_id"], "title": i["product_title"]},
                            "quantity": i["quantity"],
                            "price_per_item": {"amount_micros": i["price_micros"], "currency_code": "USD"},
                        }
                        for i in item_list
                    ],
                    "currency": "USD",
                }

            r = httpx.post(f"{API_BASE}/ucp/checkout-sessions", json=_build_body(items), headers=headers, timeout=10)

            if r.status_code == 422:
                detail = r.json().get("detail", {})
                msgs = detail.get("messages", []) if isinstance(detail, dict) else []
                if any(m.get("code") == "PRODUCT_NOT_FOUND" for m in msgs):
                    # Re-search each item by title to correct the product ID
                    corrected = []
                    for item in items:
                        search_r = httpx.get(
                            f"{API_BASE}/ucp/products",
                            params={"q": item["product_title"].split()[0], "limit": 5},
                            timeout=10,
                        )
                        found = search_r.json().get("products", []) if search_r.status_code == 200 else []
                        title_lower = item["product_title"].lower()
                        match = next(
                            (p for p in found if p["title"].lower() in title_lower or title_lower in p["title"].lower()),
                            None,
                        )
                        if match:
                            corrected.append({**item, "product_id": match["id"], "price_micros": match["price"]["amount_micros"]})
                        else:
                            corrected.append(item)
                    r = httpx.post(f"{API_BASE}/ucp/checkout-sessions", json=_build_body(corrected), headers=headers, timeout=10)

            return json.dumps(r.json(), indent=2)

        elif name == "create_checkout_and_complete":
            items = [dict(i) for i in args["items"]]
            for i in items:
                i["quantity"] = int(i.get("quantity", 1))
                i["price_micros"] = int(i.get("price_micros", 0))

            buyer_email = args.get("email", "")

            def _build_body(item_list):
                body = {
                    "line_items": [
                        {
                            "item": {"id": i["product_id"], "title": i["product_title"]},
                            "quantity": i["quantity"],
                            "price_per_item": {"amount_micros": i["price_micros"], "currency_code": "USD"},
                        }
                        for i in item_list
                    ],
                    "currency": "USD",
                }
                if buyer_email:
                    body["buyer"] = {"email": buyer_email}
                return body

            r1 = httpx.post(f"{API_BASE}/ucp/checkout-sessions", json=_build_body(items), headers=headers, timeout=10)
            if r1.status_code == 422:
                detail = r1.json().get("detail", {})
                msgs = detail.get("messages", []) if isinstance(detail, dict) else []
                if any(m.get("code") == "PRODUCT_NOT_FOUND" for m in msgs):
                    for item in items:
                        search_r = httpx.get(
                            f"{API_BASE}/ucp/products",
                            params={"q": (item.get("product_title") or "").split()[0], "limit": 5},
                            timeout=10,
                        )
                        found = search_r.json().get("products", []) if search_r.status_code == 200 else []
                        title_lower = (item.get("product_title") or "").lower()
                        match = next(
                            (p for p in found if p["title"].lower() in title_lower or title_lower in p["title"].lower()),
                            None,
                        )
                        if match:
                            item["product_id"] = match["id"]
                            item["price_micros"] = match["price"]["amount_micros"]
                    r1 = httpx.post(f"{API_BASE}/ucp/checkout-sessions", json=_build_body(items), headers=headers, timeout=10)
            if r1.status_code != 201:
                return json.dumps(r1.json(), indent=2)
            session = r1.json()
            session_id = session.get("id")
            if not session_id:
                return json.dumps({"error": "No session id", "response": session}, indent=2)
            addr_body = {
                "fulfillment": {
                    "address": {
                        "street_address": args["street"],
                        "locality": args["city"],
                        "administrative_area": args["state"],
                        "postal_code": args["postal_code"],
                        "country_code": args.get("country", "US"),
                    }
                }
            }
            r2 = httpx.put(
                f"{API_BASE}/ucp/checkout-sessions/{session_id}",
                json=addr_body,
                headers=headers,
                timeout=10,
            )
            if r2.status_code != 200:
                return json.dumps(r2.json(), indent=2)
            r3 = httpx.post(
                f"{API_BASE}/ucp/checkout-sessions/{session_id}/complete",
                json={
                    "payment_data": {
                        "instrument": {"type": "PAYMENT_TOKEN", "token": "demo-google-pay-token-xyz"},
                        "billing_address": {
                            "street_address": args["street"],
                            "locality": args["city"],
                            "administrative_area": args["state"],
                            "postal_code": args["postal_code"],
                            "country_code": args.get("country", "US"),
                        },
                    }
                },
                headers=headers,
                timeout=10,
            )
            return json.dumps(r3.json(), indent=2)

        elif name == "get_session":
            r = httpx.get(
                f"{API_BASE}/ucp/checkout-sessions/{args['session_id']}",
                headers={"Authorization": headers["Authorization"]},
                timeout=10,
            )
            return json.dumps(r.json(), indent=2)

        elif name == "get_order_status":
            auth = {"Authorization": headers["Authorization"]}
            order_id = args.get("order_id", "").strip()
            email = args.get("email", "").strip()
            # Prefer email when available — more reliable than order_id
            if email:
                r = httpx.get(f"{API_BASE}/ucp/orders", params={"email": email, "limit": 10}, headers=auth, timeout=10)
                data = r.json()
                if r.status_code == 200 and data.get("orders"):
                    # If we also have order_id, try to return that specific order's details
                    if order_id:
                        match = next((o for o in data["orders"] if str(o.get("id")) == str(order_id)), None)
                        if match:
                            detail = httpx.get(f"{API_BASE}/ucp/orders/{order_id}", headers=auth, timeout=10)
                            if detail.status_code == 200:
                                return json.dumps(detail.json(), indent=2)
                    return json.dumps(data, indent=2)
                return json.dumps(data, indent=2)
            elif order_id:
                r = httpx.get(f"{API_BASE}/ucp/orders/{order_id}", headers=auth, timeout=10)
                if r.status_code == 404:
                    return json.dumps({
                        "error": "ORDER_NOT_FOUND",
                        "message": f"Order {order_id} not found. If you have the buyer's email from checkout, retry get_order_status with email instead.",
                    }, indent=2)
                return json.dumps(r.json(), indent=2)
            else:
                return json.dumps({"error": "Provide order_id or email to look up an order."})

    except Exception as e:
        return json.dumps({"error": str(e)})

    return json.dumps({"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# Gradio chat handler
# ---------------------------------------------------------------------------

# Single demo-scoped token — refreshed lazily, no gr.State needed
_demo_token: str = ""


def chat(message: str, history: list):
    # history is list[dict] with keys "role" and "content" (Gradio 5 messages format)
    global _demo_token
    if not GEMINI_API_KEY:
        yield history + [{"role": "user", "content": message}, {"role": "assistant", "content": "⚠️ Set GEMINI_API_KEY in .env to use the demo."}]
        return

    # Always get a fresh token — the API regenerates its signing key on restart,
    # which invalidates any cached token and causes 401s mid-conversation.
    _demo_token = get_token()

    model = genai.GenerativeModel(
        model_name=os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
        tools=[{"function_declarations": TOOLS}],
        system_instruction=(
            "You are a helpful shopping assistant for Woogent, a WooCommerce store. "
            "You have access to the store's UCP API. Help users find and purchase products. "
            "When a user wants to buy something:\n"
            "1. Use search_products to find matching items\n"
            "2. Present options clearly with prices\n"
            "3. When they confirm a product (e.g. 'sure', 'yes', 'I'll take it'), CONFIRM THE TOTAL PRICE "
            "including tax (8.25%) before proceeding. Say something like 'Great choice! That's $X plus tax, "
            "totaling approximately $Y. Ready to proceed?'\n"
            "4. Once they confirm the price, ask for their shipping address AND email address. "
            "The email is required so they can track their order in the store.\n"
            "5. When the user provides address and email, use create_checkout_and_complete with items, address, and email. "
            "This creates the session, sets the address, and places the order in one call.\n"
            "IMPORTANT: When calling search_products, pass only a SHORT product keyword as 'q' "
            "(e.g. 'sunglasses', 'hoodie', 'shirt') — never the full user sentence. "
            "Extract the product noun from the user's message before searching. "
            "IMPORTANT: When you present a product to the user, ALWAYS include its numeric ID in your response "
            "like this: 'Sunglasses (ID: 18) — $90.00'. This is critical because you need the exact ID "
            "when creating a checkout session. If you are about to create a checkout session but are not "
            "100% sure of the product ID, call search_products again first to confirm it. "
            "When listing ALL products, paginate: call search_products with offset=0, then offset=10, offset=20, etc. "
            "until a page returns fewer results than the limit (meaning you've reached the end). "
            "Always show prices in dollars (divide amount_micros by 1,000,000). "
            "Be conversational and helpful. When you make API calls, briefly explain what you're doing. "
            "When an order is complete, extract the order ID from the 'order' object in the JSON response — "
            "specifically response['order']['id']. Say 'Your order number is [that exact ID]'. "
            "CRITICAL: Never invent or guess an order number. If you cannot find order.id in the response, "
            "say 'Your order has been placed' without mentioning a number. "
            "Mention that they can track their order using the email they provided. "
            "When the user asks to track or check their order status, ALWAYS use get_order_status with the "
            "buyer's email (from the checkout) — it is more reliable than order_id. Pass both email and order_id if you have both."
        ),
    )

    # Build conversation history for Gemini from Gradio messages format
    gemini_history = []
    for h in history:
        role = "model" if h["role"] == "assistant" else "user"
        gemini_history.append({"role": role, "parts": [h["content"]]})

    chat_session = model.start_chat(history=gemini_history)

    response_text = ""
    tool_log = []

    def _thinking_html(log, status_line=""):
        n = len(log)
        label = f"⚙️ {n} API call{'s' if n != 1 else ''}" if n else "⚙️ Thinking…"
        body = "\n\n".join(log)
        status_block = f"\n\n{status_line}" if status_line else ""
        return f'<details class="wg-thinking-block"><summary class="wg-thinking">{label}</summary>\n\n{body}{status_block}\n\n</details>'

    new_history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": '<details><summary class="wg-thinking">⚙️ Thinking…</summary>\n\n…\n\n</details>'},
    ]

    # Show user message + thinking indicator immediately
    yield new_history

    # Agentic loop: keep processing tool calls until Gemini gives a final text response
    current_message = message
    while True:
        response = chat_session.send_message(current_message)
        candidate = response.candidates[0]
        content = candidate.content

        # Check for tool calls
        tool_calls = [part for part in content.parts if hasattr(part, "function_call") and part.function_call.name]

        if not tool_calls:
            # Final text response
            visible_parts = []
            for part in content.parts:
                if not hasattr(part, "text") or not part.text:
                    continue
                # Gemini can emit internal thought text parts; don't show those as user-facing output.
                if getattr(part, "thought", False):
                    continue
                txt = part.text.strip()
                if "an internal thought process of the model to generate the next response" in txt.lower():
                    continue
                visible_parts.append(part.text)
            response_text = _format_assistant_text("\n".join(p.strip() for p in visible_parts if p.strip()))
            if not response_text:
                response_text = "Done."
            break

        # Execute all tool calls and collect results
        tool_results = []
        for part in tool_calls:
            fc = part.function_call
            tool_name = fc.name
            raw_args = _to_json_safe(fc.args) if fc.args else {}
            tool_args = raw_args if isinstance(raw_args, dict) else {}

            args_inline = ", ".join(f"{k}: {json.dumps(v)}" for k, v in tool_args.items()) if tool_args else ""
            tool_log.append(f"🔧 `{tool_name}` {args_inline}")
            new_history[-1]["content"] = _thinking_html(tool_log, "⏳ *Calling API...*")
            yield new_history

            result = execute_tool(tool_name, tool_args, _demo_token)

            try:
                rd = json.loads(result)
                if "error" in rd:
                    status = f"❌ {rd['error']}"
                elif "products" in rd:
                    status = f"✅ {len(rd['products'])} product(s) found"
                elif "line_items" in rd and "totals" in rd:
                    status = f"✅ {len(rd['line_items'])} cart item(s)"
                elif "id" in rd and "status" in rd:
                    status = f"✅ session `{rd['id']}` ({rd['status']})"
                else:
                    status = "✅ done"
            except Exception:
                status = "✅ done"

            tool_log[-1] += f" → {status}"
            new_history[-1]["content"] = _thinking_html(tool_log, "⏳ *Processing...*")
            yield new_history


            tool_results.append(
                genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=tool_name,
                        response={"result": result},
                    )
                )
            )

        current_message = tool_results

    # Finalise: collapse the thinking block, append response as separate message
    if tool_log:
        new_history[-1]["content"] = _thinking_html(tool_log)
        # Keep a light visual gap before final assistant response.
        new_history.append({"role": "assistant", "content": f"\n{response_text}"})
    else:
        new_history[-1] = {"role": "assistant", "content": response_text}

    yield new_history


# ---------------------------------------------------------------------------
# Gradio UI
# ---------------------------------------------------------------------------

EXAMPLE_PROMPTS = [
    "What products do you have?",
    "Show me hoodies under $50",
    "What's the cheapest item in the store?",
    "Buy a hoodie, ship to 123 Main St, Champaign IL 61820, email: demo@example.com",
]

_THINKING_JS = """
() => {
  (function() {
    // Map from thinking-block index → user-set open state
    // Index = position among all wg-thinking <details> in the chatbot (stable across re-renders)
    const stateMap = {};

    function isThinking(el) {
      return el.tagName === 'DETAILS' && el.querySelector('summary.wg-thinking');
    }

    function getThinkingIndex(el) {
      const chatbot = document.querySelector('.chatbot');
      if (!chatbot) return -1;
      const all = Array.from(chatbot.querySelectorAll('details')).filter(isThinking);
      return all.indexOf(el);
    }

    // Record user intent whenever they toggle a thinking block
    document.addEventListener('toggle', function(e) {
      if (!isThinking(e.target)) return;
      const idx = getThinkingIndex(e.target);
      if (idx >= 0) stateMap[idx] = e.target.open;
    }, true);

    // When Gradio streams a new version of the element, restore the saved state
    const observer = new MutationObserver(function(mutations) {
      for (const mut of mutations) {
        for (const node of mut.addedNodes) {
          if (node.nodeType !== 1) continue;
          const candidates = Array.from(
            node.tagName === 'DETAILS' ? [node] : node.querySelectorAll('details')
          ).filter(isThinking);
          for (const d of candidates) {
            const idx = getThinkingIndex(d);
            if (idx >= 0 && stateMap[idx] !== undefined) {
              d.open = stateMap[idx];
            }
          }
        }
      }
    });

    function init() {
      const chatbot = document.querySelector('.chatbot');
      if (!chatbot) { setTimeout(init, 300); return; }
      observer.observe(chatbot, { childList: true, subtree: true });
    }
    init();
  })();
}
"""

with gr.Blocks(
    title="Gemini Shopping Agent",
    theme=gr.themes.Soft(primary_hue="purple", font=gr.themes.GoogleFont("Inter")),
    js=_THINKING_JS,
    css="""
        html, body { background: var(--body-background-fill) !important; }
        .gradio-container {
            max-width: 920px !important;
            margin: 0 auto !important;
            padding: 1rem 1.25rem 1.5rem !important;
        }
        /* Gradient header banner */
        .app-header {
            background: linear-gradient(135deg, #4c1d95 0%, #6d28d9 55%, #7c3aed 100%);
            border-radius: 14px;
            padding: 1.1rem 1.5rem 1rem;
            margin-bottom: 0.75rem;
        }
        .app-header * { color: white !important; }
        .app-header h2 {
            font-size: 1.5rem !important;
            font-weight: 700 !important;
            margin: 0 0 0.2rem !important;
        }
        .app-header p { font-size: 0.88rem !important; margin: 0 !important; opacity: 0.88; }
        /* Example prompt chips */
        .examples-row {
            flex-wrap: wrap !important;
            gap: 6px !important;
            margin: 0.5rem 0 0.15rem !important;
        }
        .examples-row button {
            font-size: 0.78rem !important;
            padding: 0.3rem 0.9rem !important;
            border-radius: 999px !important;
            background: white !important;
            border: 1.5px solid #ddd6fe !important;
            color: #5b21b6 !important;
            white-space: nowrap !important;
            height: auto !important;
            min-width: unset !important;
            flex: 0 1 auto !important;
            box-shadow: none !important;
        }
        .examples-row button:hover {
            background: #f5f3ff !important;
            border-color: #7c3aed !important;
        }
        /* Send row */
        .send-row { gap: 8px !important; margin-top: 0.5rem !important; align-items: flex-end !important; }
        /* Hide Gradio footer */
        footer { display: none !important; }
        /* Accordion arrow: right when closed, down when open */
        .label-wrap svg { transform: rotate(-90deg) !important; transition: transform 0.2s; }
        .open > .label-wrap svg { transform: rotate(0deg) !important; }
        /* Thinking block: inline <details> rendering */
        .wg-thinking-block {
            margin: 0.2rem 0 0.65rem !important;
            padding-bottom: 0.15rem !important;
        }
        summary.wg-thinking {
            cursor: pointer;
            list-style: none;
            font-size: 0.85em;
            opacity: 0.75;
            padding: 2px 0 6px;
            margin-bottom: 0.2rem;
            line-height: 1.35;
        }
        summary.wg-thinking::-webkit-details-marker { display: none; }
        summary.wg-thinking::marker { display: none; }
        details:not([open]) summary.wg-thinking::before { content: '▶  '; font-size: 0.8em; }
        details[open] summary.wg-thinking::before { content: '▼  '; font-size: 0.8em; }
        /* Hide all scrollbars globally */
        * { scrollbar-width: none !important; -ms-overflow-style: none !important; }
        *::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }
        /* Lock chatbot height — no reflow during streaming */
        .chatbot { min-height: 530px !important; max-height: 530px !important; flex-shrink: 0 !important; }
        .chatbot > div { height: 100% !important; overflow-y: auto !important; }
        /* Hide Gradio processing/ETA bar */
        .generating, .eta-bar, .progress-text, .loader { display: none !important; }
        /* Remove container padding that creates gap on the right */
        .message-wrap, .messages { padding-right: 0 !important; }
        .user-row { padding-right: 0 !important; }
        /* Wrap long lines in code blocks instead of forcing horizontal scroll */
        .bubble-wrap pre, .bubble-wrap code { white-space: pre-wrap !important; word-break: break-word !important; }
    """,
) as demo:

    with gr.Column(elem_classes="app-header"):
        gr.Markdown(
            "## Gemini Shopping Agent\n"
            "Gemini AI shopping assistant on a live WooCommerce store — "
            "watch every UCP API call happen in real time."
        )

    chatbot = gr.Chatbot(
        height=530,
        show_label=False,
        type="messages",
        layout="bubble",
        avatar_images=(
            None,
            os.path.join(os.path.dirname(__file__), "gemini_logo.png"),
        ),
        render_markdown=True,
        sanitize_html=False,
        placeholder=(
            "<div style='text-align:center;color:#9ca3af;padding:3rem 1rem'>"
            "<div style='font-size:2rem;margin-bottom:.5rem'>🛍️</div>"
            "<div>Ask Gemini to find and buy something for you.</div>"
            "</div>"
        ),
    )

    with gr.Row(elem_classes="send-row"):
        msg = gr.Textbox(
            placeholder="e.g. 'Show me hoodies under $50' or 'Buy the cheapest item'",
            show_label=False,
            scale=10,
            container=False,
            autofocus=True,
        )
        send_btn = gr.Button("Send", variant="primary", scale=0, min_width=80)
        clear_btn = gr.Button("Clear", variant="secondary", scale=0, min_width=70)

    with gr.Row(elem_classes="examples-row"):
        ex_btns = []
        for prompt in EXAMPLE_PROMPTS:
            b = gr.Button(prompt, size="sm")
            ex_btns.append((b, prompt))

    with gr.Accordion("Developer links", open=False):
        gr.Markdown(
            f"**API docs:** [{API_PUBLIC_URL}/docs]({API_PUBLIC_URL}/docs) &nbsp;·&nbsp; "
            f"**UCP manifest:** [{API_PUBLIC_URL}/.well-known/ucp]({API_PUBLIC_URL}/.well-known/ucp) &nbsp;·&nbsp; "
            f"**Spec:** [ucp.dev](https://ucp.dev)"
        )

    # Interactions
    send_btn.click(chat, [msg, chatbot], [chatbot])
    msg.submit(chat, [msg, chatbot], [chatbot])
    send_btn.click(lambda: "", None, msg)
    msg.submit(lambda: "", None, msg)
    clear_btn.click(fn=lambda: ([], ""), inputs=[], outputs=[chatbot, msg])

    for btn, prompt in ex_btns:
        btn.click(fn=lambda p=prompt: p, inputs=[], outputs=msg)


if __name__ == "__main__":
    demo.launch(
        server_name=os.getenv("GRADIO_SERVER_NAME", "127.0.0.1"),
        server_port=7860,
        share=os.getenv("GRADIO_SHARE", "false").lower() == "true",
        show_error=True,
        show_api=False,
    )
