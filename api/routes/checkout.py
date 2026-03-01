"""
UCP Checkout Session endpoints.

  POST   /ucp/checkout-sessions            — create
  GET    /ucp/checkout-sessions/{id}       — read
  PUT    /ucp/checkout-sessions/{id}       — update (address, shipping, items)
  POST   /ucp/checkout-sessions/{id}/complete — place order
  POST   /ucp/checkout-sessions/{id}/cancel  — cancel

All state-mutating endpoints require:
  - Idempotency-Key header (UUID)
  - Request-Id header
  - UCP-Agent header (agent profile URL)
  - Authorization: Bearer {token}
"""
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from db.connection import get_db
from models.ucp import (
    CheckoutSession,
    CreateSessionRequest,
    UpdateSessionRequest,
    CompleteSessionRequest,
    UCPError,
    UCPMessage,
)
from services.ucp_adapter import (
    create_checkout_session,
    update_checkout_session,
    complete_checkout_session,
    cancel_checkout_session,
)
from routes.dependencies import require_auth

router = APIRouter(prefix="/ucp/checkout-sessions", tags=["Checkout"])


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


@router.post(
    "",
    response_model=CheckoutSession,
    status_code=status.HTTP_201_CREATED,
    summary="Create a checkout session",
    description=(
        "Initiates a UCP checkout session. Items are validated against live WooCommerce "
        "inventory. Prices are sourced from WooCommerce (client-supplied prices are ignored). "
        "Returns a session in `incomplete` status.\n\n"
        "**Required headers:** `Idempotency-Key` (UUID), `Request-Id`, `UCP-Agent`\n\n"
        "**Example (curl):**\n"
        "```bash\n"
        'curl -X POST http://localhost:8000/ucp/checkout-sessions \\\n'
        '  -H "Authorization: Bearer $TOKEN" \\\n'
        '  -H "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" \\\n'
        '  -H "Request-Id: req-001" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        "  -d '{\"line_items\":[{\"item\":{\"id\":\"19\",\"title\":\"Hoodie\"},\"quantity\":1,\"price_per_item\":{\"amount_micros\":45000000}}]}'\n"
        "```"
    ),
    responses={
        201: {
            "description": "Session created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "sess_abc123",
                        "status": "incomplete",
                        "line_items": [
                            {
                                "item": {"id": "19", "title": "Hoodie with Logo"},
                                "quantity": 1,
                                "price_per_item": {"amount_micros": 45000000, "currency_code": "USD"},
                                "total_price": {"amount_micros": 45000000, "currency_code": "USD"},
                            }
                        ],
                        "totals": {
                            "subtotal": {"amount_micros": 45000000, "currency_code": "USD"},
                            "tax": {"amount_micros": 0, "currency_code": "USD"},
                            "shipping": {"amount_micros": 0, "currency_code": "USD"},
                            "total": {"amount_micros": 45000000, "currency_code": "USD"},
                        },
                        "fulfillment": None,
                        "created_at": "2026-03-01T12:00:00Z",
                    }
                }
            },
        },
        400: {"description": "Bad request (e.g. MISSING_IDEMPOTENCY_KEY)", "model": UCPError},
        401: {"description": "Missing or invalid Bearer token (MISSING_TOKEN, INVALID_TOKEN)", "model": UCPError},
    },
    dependencies=[Depends(require_auth), Depends(_validate_idempotency_key)],
)
async def create_session(
    request: CreateSessionRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", description="**Required.** UUID; same key returns same result (idempotent)."),
    request_id: Optional[str] = Header(None, alias="Request-Id", description="Client request ID for tracing."),
    ucp_agent: Optional[str] = Header(None, alias="UCP-Agent", description="Agent or client profile URL."),
    db: AsyncSession = Depends(get_db),
):
    return await create_checkout_session(request, db)


@router.get(
    "/{session_id}",
    response_model=CheckoutSession,
    summary="Get a checkout session",
    description="Retrieve the current state of a checkout session by its ID.",
    responses={
        401: {"description": "Missing or invalid Bearer token", "model": UCPError},
        404: {"description": "Session not found (SESSION_NOT_FOUND)", "model": UCPError},
    },
    dependencies=[Depends(require_auth)],
)
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)):
    from services.session_store import get_session as _get
    session = await _get(session_id, db)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UCPError(
                status="not_found",
                messages=[UCPMessage(
                    type="error",
                    code="SESSION_NOT_FOUND",
                    content=f"Checkout session '{session_id}' not found or expired.",
                    severity="fatal",
                )],
            ).model_dump(),
        )
    return session


@router.put(
    "/{session_id}",
    response_model=CheckoutSession,
    summary="Update a checkout session",
    description=(
        "Update the session with a shipping address and/or select a shipping method. "
        "Once an address is provided, available shipping options are returned in the response. "
        "The buyer can then select an option and call this endpoint again.\n\n"
        "**Required headers:** `Idempotency-Key`, `Request-Id`, `UCP-Agent`\n\n"
        "**Example — Set shipping address (curl):**\n"
        "```bash\n"
        'curl -X PUT http://localhost:8000/ucp/checkout-sessions/{session_id} \\\n'
        '  -H "Authorization: Bearer $TOKEN" \\\n'
        '  -H "Idempotency-Key: $(uuidgen)" \\\n'
        '  -H "Request-Id: req-update-001" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        '  -d \'{"fulfillment":{"address":{"street_address":"123 Main St","locality":"Champaign","administrative_area":"IL","postal_code":"61820","country_code":"US"}}}\'\n'
        "```"
    ),
    responses={
        200: {
            "description": "Session updated — shipping options returned",
            "content": {
                "application/json": {
                    "example": {
                        "id": "sess_abc123",
                        "status": "incomplete",
                        "fulfillment": {
                            "address": {
                                "street_address": "123 Main St",
                                "locality": "Champaign",
                                "administrative_area": "IL",
                                "postal_code": "61820",
                                "country_code": "US",
                            },
                            "selected_option_id": None,
                            "options": [
                                {"id": "flat_rate:1", "label": "Flat Rate", "price": {"amount_micros": 5000000, "currency_code": "USD"}},
                                {"id": "free_shipping:2", "label": "Free Shipping", "price": {"amount_micros": 0, "currency_code": "USD"}},
                            ],
                        },
                        "totals": {
                            "subtotal": {"amount_micros": 45000000, "currency_code": "USD"},
                            "tax": {"amount_micros": 3600000, "currency_code": "USD"},
                            "shipping": {"amount_micros": 0, "currency_code": "USD"},
                            "total": {"amount_micros": 48600000, "currency_code": "USD"},
                        },
                    }
                }
            },
        },
        400: {"description": "Bad request (e.g. MISSING_IDEMPOTENCY_KEY)", "model": UCPError},
        401: {"description": "Missing or invalid Bearer token", "model": UCPError},
        404: {"description": "Session not found (SESSION_NOT_FOUND)", "model": UCPError},
    },
    dependencies=[Depends(require_auth), Depends(_validate_idempotency_key)],
)
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", description="**Required.** UUID for idempotent requests."),
    request_id: Optional[str] = Header(None, alias="Request-Id", description="Client request ID for tracing."),
    ucp_agent: Optional[str] = Header(None, alias="UCP-Agent", description="Agent or client profile URL."),
    db: AsyncSession = Depends(get_db),
):
    return await update_checkout_session(session_id, request, db)


@router.post(
    "/{session_id}/complete",
    response_model=CheckoutSession,
    summary="Complete a checkout session",
    description=(
        "Finalizes the checkout: validates stock one final time, processes payment via Stripe "
        "(if configured), creates a WooCommerce order, and transitions the session to `completed` status.\n\n"
        "**Required headers:** `Idempotency-Key`, `Request-Id`, `UCP-Agent`\n\n"
        "**Example (curl):**\n"
        "```bash\n"
        'curl -X POST http://localhost:8000/ucp/checkout-sessions/{session_id}/complete \\\n'
        '  -H "Authorization: Bearer $TOKEN" \\\n'
        '  -H "Idempotency-Key: $(uuidgen)" \\\n'
        '  -H "Request-Id: req-complete-001" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        '  -d \'{"payment_data":{"instrument":{"type":"PAYMENT_TOKEN","token":"tok_visa"}}}\'\n'
        "```"
    ),
    responses={
        200: {
            "description": "Checkout completed — order created",
            "content": {
                "application/json": {
                    "example": {
                        "id": "sess_abc123",
                        "status": "completed",
                        "line_items": [
                            {
                                "item": {"id": "19", "title": "Hoodie with Logo"},
                                "quantity": 1,
                                "price_per_item": {"amount_micros": 45000000, "currency_code": "USD"},
                                "total_price": {"amount_micros": 45000000, "currency_code": "USD"},
                            }
                        ],
                        "totals": {
                            "subtotal": {"amount_micros": 45000000, "currency_code": "USD"},
                            "tax": {"amount_micros": 3600000, "currency_code": "USD"},
                            "shipping": {"amount_micros": 5000000, "currency_code": "USD"},
                            "total": {"amount_micros": 53600000, "currency_code": "USD"},
                        },
                        "order": {
                            "id": "127",
                            "status": "wc-processing",
                            "view_url": "http://localhost:8080/checkout/order-received/127/",
                        },
                        "updated_at": "2026-03-01T12:05:00Z",
                    }
                }
            },
        },
        400: {"description": "Bad request (e.g. MISSING_IDEMPOTENCY_KEY, out of stock)", "model": UCPError},
        401: {"description": "Missing or invalid Bearer token", "model": UCPError},
        404: {"description": "Session not found (SESSION_NOT_FOUND)", "model": UCPError},
    },
    dependencies=[Depends(require_auth), Depends(_validate_idempotency_key)],
)
async def complete_session(
    session_id: str,
    request: CompleteSessionRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", description="**Required.** UUID for idempotent requests."),
    request_id: Optional[str] = Header(None, alias="Request-Id", description="Client request ID for tracing."),
    ucp_agent: Optional[str] = Header(None, alias="UCP-Agent", description="Agent or client profile URL."),
    db: AsyncSession = Depends(get_db),
):
    return await complete_checkout_session(session_id, request, db)


@router.post(
    "/{session_id}/cancel",
    response_model=CheckoutSession,
    summary="Cancel a checkout session",
    description=(
        "Cancel an active checkout session. Completed sessions cannot be cancelled. "
        "**Required headers:** Idempotency-Key, Request-Id, UCP-Agent.\n\n"
        "**Example (curl):**\n"
        "```bash\n"
        'curl -X POST http://localhost:8000/ucp/checkout-sessions/{session_id}/cancel \\\n'
        '  -H "Authorization: Bearer $TOKEN" \\\n'
        '  -H "Idempotency-Key: $(uuidgen)" \\\n'
        '  -H "Request-Id: req-cancel-001"\n'
        "```"
    ),
    responses={
        200: {
            "description": "Session cancelled",
            "content": {
                "application/json": {
                    "example": {
                        "id": "sess_abc123",
                        "status": "cancelled",
                        "line_items": [],
                        "totals": None,
                        "fulfillment": None,
                        "updated_at": "2026-03-01T12:10:00Z",
                    }
                }
            },
        },
        400: {"description": "Bad request (e.g. MISSING_IDEMPOTENCY_KEY)", "model": UCPError},
        401: {"description": "Missing or invalid Bearer token", "model": UCPError},
        404: {"description": "Session not found (SESSION_NOT_FOUND)", "model": UCPError},
    },
    dependencies=[Depends(require_auth), Depends(_validate_idempotency_key)],
)
async def cancel_session(
    session_id: str,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", description="**Required.** UUID for idempotent requests."),
    request_id: Optional[str] = Header(None, alias="Request-Id", description="Client request ID for tracing."),
    ucp_agent: Optional[str] = Header(None, alias="UCP-Agent", description="Agent or client profile URL."),
    db: AsyncSession = Depends(get_db),
):
    return await cancel_checkout_session(session_id, db)
