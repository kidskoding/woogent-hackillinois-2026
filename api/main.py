"""
Woogent — WooCommerce Universal Commerce Protocol (UCP) API

Implements the Google UCP spec (https://ucp.dev) on top of a WooCommerce
MySQL database, enabling AI agents (Gemini, etc.) to discover, browse,
and checkout from a WooCommerce store.

Spec version: 2026-01-11
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import text
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import get_settings
from routes.well_known import router as well_known_router
from routes.checkout import router as checkout_router
from routes.oauth import router as oauth_router
from routes.orders import router as orders_router
from routes.products import router as products_router
from routes.demo import router as demo_router

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up: import auth module to generate RSA key pair on startup
    import services.auth  # noqa: F401
    yield


app = FastAPI(
    title="Woogent UCP API",
    description="""
## WooCommerce Universal Commerce Protocol API

This API implements Google's [Universal Commerce Protocol (UCP)](https://ucp.dev)
on top of WooCommerce, enabling AI agents like Gemini to discover, browse, and
checkout from a WooCommerce store autonomously.

---

## Quick Start — Try These Now

```bash
# 1. Discover the store (no auth)
curl http://localhost:8000/.well-known/ucp

# 2. Search products (no auth)
curl "http://localhost:8000/ucp/products?q=hoodie&limit=3"

# 3. Get a token (demo mode)
TOKEN=$(curl -s -X POST http://localhost:8000/demo/token \
  -u "gemini-demo-client:gemini-demo-secret" | jq -r .access_token)

# 4. Create a checkout session
curl -X POST http://localhost:8000/ucp/checkout-sessions \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Idempotency-Key: $(uuidgen)" \\
  -H "Request-Id: req-001" \\
  -H "Content-Type: application/json" \\
  -d '{"line_items":[{"item":{"id":"19","title":"Hoodie"},"quantity":1,"price_per_item":{"amount_micros":45000000}}]}'
```

---

## How It Works

| Step | Endpoint | What happens |
|------|----------|--------------|
| 1. **Discover** | `GET /.well-known/ucp` | AI learns capabilities, payment handlers, signing keys |
| 2. **Browse** | `GET /ucp/products` | Search catalog by keyword, price, category |
| 3. **Checkout** | `POST /ucp/checkout-sessions` | Create session → set address → select shipping → complete |
| 4. **Track** | `GET /ucp/orders/{id}` | Check order status, line items, fulfillment |

---

## Authentication

**Public endpoints** (no token): Discovery (`/.well-known/*`), Products (`/ucp/products`)

**Protected endpoints** (Bearer token required): Checkout sessions, Orders

```bash
# OAuth 2.0 flow (production)
GET /oauth2/authorize?client_id=...&redirect_uri=...&response_type=code&scope=ucp:scopes:checkout_session&state=...
POST /oauth2/token  # HTTP Basic auth + authorization_code

# Demo shortcut (local dev / hackathon demo)
curl -X POST http://localhost:8000/demo/token -u "gemini-demo-client:gemini-demo-secret"
```

**Required headers on state-mutating calls:**
- `Authorization: Bearer <token>`
- `Idempotency-Key: <uuid>` — ensures at-most-once semantics
- `Request-Id: <client-trace-id>`

---

## Money Format

All prices use **`amount_micros`** (integer = currency × 1,000,000) to avoid floating-point errors:

| Display | amount_micros | currency_code |
|---------|---------------|---------------|
| $19.99 | `19990000` | `USD` |
| $45.00 | `45000000` | `USD` |
| $0.01 | `10000` | `USD` |

---

## Error Codes

All errors return UCP-format JSON:
```json
{"status": "...", "messages": [{"type": "error", "code": "...", "content": "...", "severity": "fatal|recoverable"}]}
```

| Code | HTTP | When |
|------|------|------|
| `MISSING_IDEMPOTENCY_KEY` | 400 | State-mutating call missing `Idempotency-Key` header |
| `MISSING_TOKEN` | 401 | No `Authorization: Bearer` header |
| `INVALID_TOKEN` | 401 | Token expired, revoked, or malformed |
| `SESSION_NOT_FOUND` | 404 | Checkout session ID unknown or expired |
| `PRODUCT_NOT_FOUND` | 404 | Product ID does not exist |
| `ORDER_NOT_FOUND` | 404 | Order ID does not exist |
| `CANNOT_CANCEL_ORDER` | 409 | Order already paid/shipped — cannot cancel |

---

## End-to-End Checkout Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. DISCOVER                                                         │
│    GET /.well-known/ucp → learn capabilities, payment handlers      │
├─────────────────────────────────────────────────────────────────────┤
│ 2. BROWSE                                                           │
│    GET /ucp/products?q=hoodie → find product IDs and prices         │
├─────────────────────────────────────────────────────────────────────┤
│ 3. AUTHENTICATE                                                     │
│    POST /demo/token (or full OAuth flow) → get Bearer token         │
├─────────────────────────────────────────────────────────────────────┤
│ 4. CREATE SESSION                                                   │
│    POST /ucp/checkout-sessions → status: "incomplete"               │
├─────────────────────────────────────────────────────────────────────┤
│ 5. SET ADDRESS                                                      │
│    PUT /ucp/checkout-sessions/{id} → returns shipping options       │
├─────────────────────────────────────────────────────────────────────┤
│ 6. SELECT SHIPPING                                                  │
│    PUT /ucp/checkout-sessions/{id} → selected_option_id             │
├─────────────────────────────────────────────────────────────────────┤
│ 7. COMPLETE CHECKOUT                                                │
│    POST /ucp/checkout-sessions/{id}/complete → status: "completed"  │
│    WooCommerce order created! 🎉                                    │
├─────────────────────────────────────────────────────────────────────┤
│ 8. TRACK ORDER                                                      │
│    GET /ucp/orders/{order_id} → status, tracking, line items        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Links

- **UCP Spec**: [ucp.dev](https://ucp.dev)
- **Checkout Guide**: [Google UCP Checkout](https://developers.google.com/merchant/ucp/guides/checkout/native)
- **Source**: [GitHub](https://github.com/kidskoding/woogent-hackillinois-2026)
""",
    version="1.0.0",
    contact={
        "name": "Woogent",
        "url": "https://github.com/woogent",
    },
    license_info={
        "name": "MIT",
    },
    openapi_tags=[
        {
            "name": "Discovery",
            "description": "Entry points for AI agents. Start here to learn what the store supports.",
            "externalDocs": {"description": "UCP Discovery Spec", "url": "https://ucp.dev"},
        },
        {
            "name": "Products",
            "description": "Browse the product catalog. **No auth required.** Filter by keyword, price, category.",
        },
        {
            "name": "Checkout",
            "description": (
                "Create and manage checkout sessions. "
                "**State machine:** `incomplete` → `pending_payment` → `completed` (or `cancelled`). "
                "Sessions expire after 24 hours if not completed."
            ),
            "externalDocs": {"description": "UCP Checkout Guide", "url": "https://developers.google.com/merchant/ucp/guides/checkout/native"},
        },
        {
            "name": "Orders",
            "description": "Retrieve order status and details. Cancel unpaid orders.",
        },
        {
            "name": "OAuth 2.0",
            "description": "Authorization Code grant (RFC 6749). RS256-signed JWTs.",
            "externalDocs": {"description": "RFC 6749", "url": "https://datatracker.ietf.org/doc/html/rfc6749"},
        },
        {
            "name": "Demo",
            "description": "Shortcut token endpoint for hackathon demos. Only active when `DEMO_MODE=true`.",
        },
        {
            "name": "System",
            "description": "Health checks and service metadata.",
        },
    ],
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ---------------------------------------------------------------------------
# CORS — allow AI agents from any origin
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Request-Id"],
)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(well_known_router)
app.include_router(oauth_router)
app.include_router(demo_router)
app.include_router(products_router)
app.include_router(checkout_router)
app.include_router(orders_router)


# ---------------------------------------------------------------------------
# Root + Health
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "Woogent UCP API",
        "ucp_version": settings.ucp_version,
        "discovery": f"{settings.wc_domain}/.well-known/ucp",
        "docs": f"{settings.wc_domain}/docs",
        "spec": "https://ucp.dev",
    }


@app.get(
    "/health",
    tags=["System"],
    summary="Health check",
    description="Returns service status and DB connectivity. Use this to verify the API is running.",
)
async def health():
    from db.connection import engine
    db_ok = False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "unavailable",
        "ucp_version": settings.ucp_version,
    }


# ---------------------------------------------------------------------------
# Global exception handler — always return UCP error format
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "status": "internal_error",
            "messages": [
                {
                    "type": "error",
                    "code": "INTERNAL_SERVER_ERROR",
                    "content": "An unexpected error occurred.",
                    "severity": "fatal",
                }
            ],
        },
    )
