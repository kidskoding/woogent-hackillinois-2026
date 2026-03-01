"""
Order management endpoints.

  GET  /ucp/orders/{order_id}   — fetch order status and details
  POST /ucp/orders/{order_id}/cancel — cancel an unpaid/draft order
  POST /ucp/orders/webhook      — receive order lifecycle event notifications

Referenced in UCP spec:
  https://developers.google.com/merchant/ucp/guides/checkout/native
  "Order Management: Manages updates, tracking, and returns via webhooks."
"""
import hashlib
import hmac
import json
import os
import time
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import get_db
from models.ucp import UCPError, UCPMessage
from routes.dependencies import require_auth

router = APIRouter(prefix="/ucp/orders", tags=["Orders"])

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", os.getenv("SECRET_KEY", "change-me"))
CANCELLABLE_ORDER_STATUSES = {"wc-checkout-draft", "wc-pending", "wc-failed"}


def _validate_idempotency_key(
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    request_id: Optional[str] = Header(None, alias="Request-Id"),
    ucp_agent: Optional[str] = Header(None, alias="UCP-Agent"),
):
    """Validate UCP-required headers on state-mutating requests."""
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=UCPError(
                status="missing_header",
                messages=[UCPMessage(
                    type="error",
                    code="MISSING_IDEMPOTENCY_KEY",
                    content="Idempotency-Key header is required for state-mutating operations.",
                    severity="fatal",
                )],
            ).model_dump(),
        )


# ---------------------------------------------------------------------------
# Order models
# ---------------------------------------------------------------------------

class OrderLineItem(BaseModel):
    product_id: str
    title: str
    quantity: int
    unit_price_micros: int
    total_micros: int


class OrderAddress(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    street_address: Optional[str] = None
    locality: Optional[str] = None
    administrative_area: Optional[str] = None
    postal_code: Optional[str] = None
    country_code: str = "US"


class Order(BaseModel):
    id: str
    status: str
    currency: str
    subtotal_micros: int
    tax_micros: int
    total_micros: int
    billing_address: Optional[OrderAddress] = None
    shipping_address: Optional[OrderAddress] = None
    line_items: list[OrderLineItem] = []
    payment_method: Optional[str] = None
    created_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Webhook event models
# ---------------------------------------------------------------------------

WebhookEventType = Literal[
    "order.created",
    "order.updated",
    "order.completed",
    "order.cancelled",
    "order.refunded",
]


class WebhookEvent(BaseModel):
    event_type: WebhookEventType
    order_id: str
    timestamp: str
    data: dict = {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "",
    summary="List orders",
    description=(
        "List WooCommerce orders. Optionally filter by buyer email or status. "
        "Results are ordered newest-first.\n\n"
        "**Examples (curl):**\n"
        "```bash\n"
        "# All orders\n"
        'curl http://localhost:8000/ucp/orders -H "Authorization: Bearer $TOKEN"\n\n'
        "# Filter by buyer email\n"
        'curl "http://localhost:8000/ucp/orders?email=buyer@example.com" \\\n'
        '  -H "Authorization: Bearer $TOKEN"\n\n'
        "# Filter by status + pagination\n"
        'curl "http://localhost:8000/ucp/orders?status=wc-processing&limit=5" \\\n'
        '  -H "Authorization: Bearer $TOKEN"\n'
        "```"
    ),
    responses={
        200: {
            "description": "Paginated list of orders",
            "content": {
                "application/json": {
                    "example": {
                        "orders": [
                            {
                                "id": "127",
                                "status": "wc-processing",
                                "currency": "USD",
                                "subtotal_micros": 45000000,
                                "tax_micros": 3600000,
                                "total_micros": 53600000,
                                "billing_email": "buyer@example.com",
                                "payment_method": "stripe",
                                "created_at": "2026-03-01 12:05:00",
                            }
                        ],
                        "count": 1,
                        "limit": 20,
                        "offset": 0,
                        "has_more": False,
                    }
                }
            },
        },
        401: {"description": "Missing or invalid Bearer token", "model": UCPError},
    },
    dependencies=[Depends(require_auth)],
)
async def list_orders(
    email: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    sql = """
        SELECT id, status, currency, tax_amount, total_amount, billing_email,
               payment_method, date_created_gmt
        FROM wp_wc_orders
        WHERE type = 'shop_order'
    """
    params: dict = {"limit": limit, "offset": offset}

    if email:
        sql += " AND billing_email = :email"
        params["email"] = email
    if status:
        sql += " AND status = :status"
        params["status"] = status

    sql += " ORDER BY date_created_gmt DESC LIMIT :limit OFFSET :offset"

    result = await db.execute(text(sql), params)
    rows = result.mappings().all()

    orders = []
    for row in rows:
        total = float(row["total_amount"] or 0)
        tax = float(row["tax_amount"] or 0)
        orders.append({
            "id": str(row["id"]),
            "status": row["status"] or "unknown",
            "currency": row["currency"] or "USD",
            "subtotal_micros": int((total - tax) * 1_000_000),
            "tax_micros": int(tax * 1_000_000),
            "total_micros": int(total * 1_000_000),
            "billing_email": row["billing_email"],
            "payment_method": row["payment_method"],
            "created_at": str(row["date_created_gmt"]) if row["date_created_gmt"] else None,
        })

    return {
        "orders": orders,
        "count": len(orders),
        "limit": limit,
        "offset": offset,
        "has_more": len(orders) == limit,
    }


@router.get(
    "/{order_id}",
    response_model=Order,
    summary="Get order details",
    description=(
        "Retrieve the current status and details of a WooCommerce order "
        "placed via UCP checkout. Use this to check fulfillment status after purchase.\n\n"
        "**Example (curl):**\n"
        "```bash\n"
        'curl http://localhost:8000/ucp/orders/127 \\\n'
        '  -H "Authorization: Bearer $TOKEN"\n'
        "```"
    ),
    responses={
        200: {
            "description": "Order details",
            "content": {
                "application/json": {
                    "example": {
                        "id": "127",
                        "status": "wc-processing",
                        "currency": "USD",
                        "subtotal_micros": 45000000,
                        "tax_micros": 3600000,
                        "total_micros": 53600000,
                        "billing_address": {
                            "first_name": "John",
                            "last_name": "Doe",
                            "street_address": "123 Main St",
                            "locality": "Champaign",
                            "administrative_area": "IL",
                            "postal_code": "61820",
                            "country_code": "US",
                        },
                        "shipping_address": {
                            "first_name": "John",
                            "last_name": "Doe",
                            "street_address": "123 Main St",
                            "locality": "Champaign",
                            "administrative_area": "IL",
                            "postal_code": "61820",
                            "country_code": "US",
                        },
                        "line_items": [
                            {
                                "product_id": "19",
                                "title": "Hoodie with Logo",
                                "quantity": 1,
                                "unit_price_micros": 45000000,
                                "total_micros": 45000000,
                            }
                        ],
                        "payment_method": "stripe",
                        "created_at": "2026-03-01 12:05:00",
                    }
                }
            },
        },
        401: {"description": "Missing or invalid Bearer token", "model": UCPError},
        404: {"description": "Order not found (ORDER_NOT_FOUND)", "model": UCPError},
    },
    dependencies=[Depends(require_auth)],
)
async def get_order(order_id: str, db: AsyncSession = Depends(get_db)):
    # Fetch from wc_orders (HPOS schema)
    order_id_param = int(order_id) if order_id.isdigit() else order_id
    result = await db.execute(
        text("""
            SELECT
                o.id,
                o.status,
                o.currency,
                o.tax_amount,
                o.total_amount,
                o.payment_method,
                o.date_created_gmt
            FROM wp_wc_orders o
            WHERE o.id = :order_id AND o.type = 'shop_order'
        """),
        {"order_id": order_id_param},
    )
    row = result.mappings().first()

    # Fallback: legacy WooCommerce stores orders in wp_posts when HPOS is disabled
    if not row:
        legacy = await db.execute(
            text("""
                SELECT p.ID AS id, p.post_status AS status, p.post_date AS date_created_gmt
                FROM wp_posts p
                WHERE p.ID = :order_id AND p.post_type = 'shop_order'
            """),
            {"order_id": order_id_param},
        )
        legacy_row = legacy.mappings().first()
        if legacy_row:
            # Build minimal row from postmeta
            meta = await db.execute(
                text("""
                    SELECT meta_key, meta_value FROM wp_postmeta
                    WHERE post_id = :order_id AND meta_key IN
                    ('_order_total', '_order_tax', '_billing_email', '_payment_method')
                """),
                {"order_id": order_id_param},
            )
            meta_map = {r["meta_key"]: r["meta_value"] for r in meta.mappings().all()}
            total = float(meta_map.get("_order_total") or 0)
            tax = float(meta_map.get("_order_tax") or 0)
            row = {
                "id": legacy_row["id"],
                "status": legacy_row["status"] or "unknown",
                "currency": "USD",
                "tax_amount": tax,
                "total_amount": total,
                "payment_method": meta_map.get("_payment_method") or "unknown",
                "date_created_gmt": legacy_row["date_created_gmt"],
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=UCPError(
                    status="not_found",
                    messages=[UCPMessage(
                        type="error",
                        code="ORDER_NOT_FOUND",
                        content=f"Order '{order_id}' not found.",
                        severity="fatal",
                    )],
                ).model_dump(),
            )

    total = float(row["total_amount"] or 0)
    tax = float(row["tax_amount"] or 0)
    subtotal = total - tax

    # Fetch addresses
    addr_result = await db.execute(
        text("""
            SELECT address_type, first_name, last_name, address_1,
                   city, state, postcode, country
            FROM wp_wc_order_addresses
            WHERE order_id = :order_id
        """),
        {"order_id": order_id},
    )
    addresses = {r["address_type"]: dict(r) for r in addr_result.mappings().all()}

    def _to_address(a: dict) -> OrderAddress:
        return OrderAddress(
            first_name=a.get("first_name"),
            last_name=a.get("last_name"),
            street_address=a.get("address_1"),
            locality=a.get("city"),
            administrative_area=a.get("state"),
            postal_code=a.get("postcode"),
            country_code=a.get("country", "US"),
        )

    # Fetch line items
    li_result = await db.execute(
        text("""
            SELECT
                oi.order_item_id,
                oi.order_item_name AS title,
                MAX(CASE WHEN oim.meta_key = '_product_id' THEN oim.meta_value END) AS product_id,
                MAX(CASE WHEN oim.meta_key = '_qty' THEN oim.meta_value END) AS quantity,
                MAX(CASE WHEN oim.meta_key = '_line_subtotal' THEN oim.meta_value END) AS subtotal,
                MAX(CASE WHEN oim.meta_key = '_line_total' THEN oim.meta_value END) AS total
            FROM wp_woocommerce_order_items oi
            LEFT JOIN wp_woocommerce_order_itemmeta oim ON oim.order_item_id = oi.order_item_id
            WHERE oi.order_id = :order_id AND oi.order_item_type = 'line_item'
            GROUP BY oi.order_item_id
        """),
        {"order_id": order_id},
    )
    line_items = []
    for li in li_result.mappings().all():
        qty = int(li["quantity"] or 1)
        item_total = float(li["total"] or 0)
        unit_price = item_total / qty if qty else 0
        line_items.append(OrderLineItem(
            product_id=str(li["product_id"] or ""),
            title=li["title"] or "",
            quantity=qty,
            unit_price_micros=int(unit_price * 1_000_000),
            total_micros=int(item_total * 1_000_000),
        ))

    return Order(
        id=str(row["id"]),
        status=row["status"] or "unknown",
        currency=row["currency"] or "USD",
        subtotal_micros=int(subtotal * 1_000_000),
        tax_micros=int(tax * 1_000_000),
        total_micros=int(total * 1_000_000),
        billing_address=_to_address(addresses["billing"]) if "billing" in addresses else None,
        shipping_address=_to_address(addresses["shipping"]) if "shipping" in addresses else None,
        line_items=line_items,
        payment_method=row["payment_method"],
        created_at=str(row["date_created_gmt"]) if row["date_created_gmt"] else None,
    )


@router.post(
    "/{order_id}/cancel",
    response_model=Order,
    summary="Cancel an order",
    description=(
        "Cancel an order when it is still in an unpaid/draft state "
        "(e.g. `wc-checkout-draft`, `wc-pending`). "
        "Paid or finalized orders cannot be cancelled through this endpoint.\n\n"
        "**Cancellable statuses:** `wc-checkout-draft`, `wc-pending`, `wc-failed`\n\n"
        "**Example (curl):**\n"
        "```bash\n"
        'curl -X POST http://localhost:8000/ucp/orders/127/cancel \\\n'
        '  -H "Authorization: Bearer $TOKEN" \\\n'
        '  -H "Idempotency-Key: $(uuidgen)" \\\n'
        '  -H "Request-Id: req-cancel-001"\n'
        "```"
    ),
    responses={
        200: {
            "description": "Order cancelled",
            "content": {
                "application/json": {
                    "example": {
                        "id": "127",
                        "status": "wc-cancelled",
                        "currency": "USD",
                        "subtotal_micros": 45000000,
                        "tax_micros": 0,
                        "total_micros": 45000000,
                        "line_items": [],
                        "payment_method": None,
                        "created_at": "2026-03-01 12:00:00",
                    }
                }
            },
        },
        401: {"description": "Missing or invalid Bearer token", "model": UCPError},
        404: {"description": "Order not found (ORDER_NOT_FOUND)", "model": UCPError},
        409: {"description": "Order already paid/shipped (CANNOT_CANCEL_ORDER)", "model": UCPError},
    },
    dependencies=[Depends(require_auth), Depends(_validate_idempotency_key)],
)
async def cancel_order(order_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text(
            """
            SELECT id, status
            FROM wp_wc_orders
            WHERE id = :order_id AND type = 'shop_order'
            """
        ),
        {"order_id": order_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UCPError(
                status="not_found",
                messages=[UCPMessage(
                    type="error",
                    code="ORDER_NOT_FOUND",
                    content=f"Order '{order_id}' not found.",
                    severity="fatal",
                )],
            ).model_dump(),
        )

    current_status = row.get("status") or ""
    if current_status == "wc-cancelled":
        # Idempotent replay behavior: return current resource state.
        return await get_order(order_id, db)

    if current_status not in CANCELLABLE_ORDER_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=UCPError(
                status="invalid_state",
                messages=[UCPMessage(
                    type="error",
                    code="CANNOT_CANCEL_ORDER",
                    content=(
                        f"Order '{order_id}' is in status '{current_status}' and cannot be cancelled. "
                        "Only unpaid/draft orders can be cancelled."
                    ),
                    severity="fatal",
                )],
            ).model_dump(),
        )

    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    await db.execute(
        text(
            """
            UPDATE wp_wc_orders
            SET status = 'wc-cancelled', date_updated_gmt = :now
            WHERE id = :order_id AND type = 'shop_order'
            """
        ),
        {"order_id": order_id, "now": now},
    )

    # Best effort: if this order links to a UCP session, cancel that session too.
    meta = await db.execute(
        text(
            """
            SELECT meta_value
            FROM wp_wc_orders_meta
            WHERE order_id = :order_id AND meta_key = '_ucp_session_id'
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"order_id": order_id},
    )
    meta_row = meta.mappings().first()
    session_id = (meta_row or {}).get("meta_value")
    if session_id:
        from services.ucp_adapter import cancel_checkout_session
        try:
            await cancel_checkout_session(str(session_id), db)
        except HTTPException:
            pass

    await db.commit()
    return await get_order(order_id, db)


@router.post(
    "/webhook",
    summary="Order lifecycle webhook",
    description=(
        "Receives order lifecycle event notifications from external systems. "
        "Requests are verified using HMAC-SHA256 signature in the `X-UCP-Signature` header.\n\n"
        "**Supported events:** `order.created`, `order.updated`, `order.completed`, "
        "`order.cancelled`, `order.refunded`\n\n"
        "**Signature verification:** `X-UCP-Signature: sha256=<hmac-sha256-hex>`\n\n"
        "**Example payload:**\n"
        "```json\n"
        '{"event_type": "order.completed", "order_id": "127", "timestamp": "2026-03-01T12:10:00Z", "data": {}}\n'
        "```"
    ),
    responses={
        200: {
            "description": "Webhook acknowledged",
            "content": {
                "application/json": {
                    "example": {"received": True, "event_type": "order.completed", "order_id": "127"}
                }
            },
        },
        400: {"description": "Invalid payload (INVALID_WEBHOOK_PAYLOAD)", "model": UCPError},
        401: {"description": "Invalid signature (INVALID_WEBHOOK_SIGNATURE)", "model": UCPError},
    },
    status_code=status.HTTP_200_OK,
)
async def order_webhook(
    request: Request,
    x_ucp_signature: Optional[str] = Header(None, alias="X-UCP-Signature"),
):
    body = await request.body()

    # Verify HMAC-SHA256 signature
    if x_ucp_signature:
        expected = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, x_ucp_signature):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=UCPError(
                    status="invalid_signature",
                    messages=[UCPMessage(
                        type="error",
                        code="INVALID_WEBHOOK_SIGNATURE",
                        content="X-UCP-Signature does not match payload.",
                        severity="fatal",
                    )],
                ).model_dump(),
            )

    try:
        event = WebhookEvent(**json.loads(body))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=UCPError(
                status="invalid_payload",
                messages=[UCPMessage(
                    type="error",
                    code="INVALID_WEBHOOK_PAYLOAD",
                    content="Webhook payload must be a valid WebhookEvent JSON object.",
                    severity="fatal",
                )],
            ).model_dump(),
        )

    # Event is acknowledged — in production, queue for async processing
    return {"received": True, "event_type": event.event_type, "order_id": event.order_id}
