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
        "Returns a session in `incomplete` status."
    ),
    dependencies=[Depends(require_auth), Depends(_validate_idempotency_key)],
)
async def create_session(
    request: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
):
    return await create_checkout_session(request, db)


@router.get(
    "/{session_id}",
    response_model=CheckoutSession,
    summary="Get a checkout session",
    description="Retrieve the current state of a checkout session by its ID.",
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
        "The buyer can then select an option and call this endpoint again."
    ),
    dependencies=[Depends(require_auth), Depends(_validate_idempotency_key)],
)
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
    db: AsyncSession = Depends(get_db),
):
    return await update_checkout_session(session_id, request, db)


@router.post(
    "/{session_id}/complete",
    response_model=CheckoutSession,
    summary="Complete a checkout session",
    description=(
        "Finalizes the checkout: validates stock one final time, creates a WooCommerce order "
        "in the database, and transitions the session to `completed` status. "
        "Payment token is accepted but not charged in this demo."
    ),
    dependencies=[Depends(require_auth), Depends(_validate_idempotency_key)],
)
async def complete_session(
    session_id: str,
    request: CompleteSessionRequest,
    db: AsyncSession = Depends(get_db),
):
    return await complete_checkout_session(session_id, request, db)


@router.post(
    "/{session_id}/cancel",
    response_model=CheckoutSession,
    summary="Cancel a checkout session",
    description="Cancel an active checkout session. Completed sessions cannot be cancelled.",
    dependencies=[Depends(require_auth), Depends(_validate_idempotency_key)],
)
async def cancel_session(session_id: str, db: AsyncSession = Depends(get_db)):
    return await cancel_checkout_session(session_id, db)
