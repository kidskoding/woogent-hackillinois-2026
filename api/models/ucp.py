"""
UCP (Universal Commerce Protocol) Pydantic models.

All monetary values follow the UCP spec: amount_micros (integer, currency × 1,000,000).
Example: $19.99 → amount_micros=19990000, currency_code="USD"

Spec reference: https://ucp.dev/specification/checkout-rest/
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

class Money(BaseModel):
    """Monetary value per UCP spec — integer micros avoids float rounding."""

    amount_micros: int = Field(
        ...,
        description="Amount in millionths of the currency unit. $1.00 = 1_000_000.",
        examples=[19990000],
    )
    currency_code: str = Field(
        default="USD",
        description="ISO 4217 currency code.",
        examples=["USD"],
    )


# ---------------------------------------------------------------------------
# Item / Line item
# ---------------------------------------------------------------------------

class ItemRef(BaseModel):
    """Reference to a WooCommerce product."""

    id: str = Field(..., description="WooCommerce product post ID.", examples=["42"])
    title: str = Field(..., description="Human-readable product title.", examples=["Hoodie with Logo"])


class LineItem(BaseModel):
    """A single product line in a checkout session."""

    item: ItemRef
    quantity: int = Field(..., ge=1, description="Number of units.", examples=[2])
    price_per_item: Money = Field(..., description="Unit price at time of session creation.")
    total_price: Optional[Money] = Field(
        None, description="quantity × price_per_item. Computed by server."
    )


# ---------------------------------------------------------------------------
# Address / Fulfillment
# ---------------------------------------------------------------------------

class Address(BaseModel):
    """Postal address using UCP field names (maps to CLDR / Google standards)."""

    given_name: Optional[str] = None
    family_name: Optional[str] = None
    street_address: str = Field(..., examples=["123 Main St"])
    locality: str = Field(..., description="City.", examples=["Champaign"])
    administrative_area: str = Field(..., description="State / province code.", examples=["IL"])
    postal_code: str = Field(..., examples=["61820"])
    country_code: str = Field(default="US", description="ISO 3166-1 alpha-2.", examples=["US"])


class ShippingOption(BaseModel):
    """An available shipping method returned after address is provided."""

    id: str = Field(..., description="Shipping method identifier.", examples=["flat_rate:1"])
    label: str = Field(..., description="Human-readable label.", examples=["Standard Shipping (5–7 days)"])
    price: Money


class Fulfillment(BaseModel):
    """Shipping address + selected or available shipping options."""

    address: Optional[Address] = None
    selected_option_id: Optional[str] = Field(
        None, description="ID of the chosen shipping method."
    )
    options: list[ShippingOption] = Field(
        default_factory=list,
        description="Available shipping methods (populated after address is set).",
    )


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

class Totals(BaseModel):
    subtotal: Money
    tax: Money
    shipping: Money = Field(default_factory=lambda: Money(amount_micros=0))
    total: Money


# ---------------------------------------------------------------------------
# Checkout Session
# ---------------------------------------------------------------------------

SessionStatus = Literal["incomplete", "pending_payment", "completed", "cancelled"]


class CheckoutSession(BaseModel):
    """
    A UCP checkout session.

    State machine:
      incomplete → pending_payment → completed
                ↘ cancelled
    """

    id: str = Field(..., description="Opaque session identifier (UUID).")
    status: SessionStatus = Field(default="incomplete")
    line_items: list[LineItem]
    totals: Optional[Totals] = None
    fulfillment: Optional[Fulfillment] = None
    buyer: Optional[BuyerInfo] = None
    payment: Optional[PaymentInfo] = None
    privacy_policy: Optional[str] = None
    terms_of_service: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    order: Optional["OrderRef"] = None


class OrderRef(BaseModel):
    """Reference to a WooCommerce order created from this session."""

    id: str = Field(..., description="WooCommerce HPOS order ID.")
    status: Optional[str] = Field(None, description="WooCommerce order status (e.g. wc-processing).")
    view_url: Optional[str] = Field(None, description="Guest-accessible order tracking URL (no login required).")


# ---------------------------------------------------------------------------
# Buyer / Payment
# ---------------------------------------------------------------------------

class BuyerInfo(BaseModel):
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None


class PaymentInstrument(BaseModel):
    type: Literal["PAYMENT_TOKEN", "CARD"] = "PAYMENT_TOKEN"
    token: str = Field(..., description="Encrypted payment token from payment handler.")


class BillingAddress(Address):
    pass


class PaymentData(BaseModel):
    instrument: PaymentInstrument
    billing_address: Optional[BillingAddress] = None


class PaymentHandlerInfo(BaseModel):
    name: str = Field(..., examples=["google_pay"])
    supported_instruments: list[str] = Field(default_factory=lambda: ["CARD", "WALLET"])


class PaymentInfo(BaseModel):
    handlers: list[PaymentHandlerInfo] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    """POST /ucp/checkout-sessions"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "line_items": [
                    {
                        "item": {"id": "19", "title": "Hoodie with Logo"},
                        "quantity": 1,
                        "price_per_item": {"amount_micros": 45000000, "currency_code": "USD"},
                    }
                ],
                "currency": "USD",
            }
        }
    )

    line_items: list[LineItem] = Field(..., min_length=1)
    currency: str = Field(default="USD")
    buyer: Optional[BuyerInfo] = None


class UpdateSessionRequest(BaseModel):
    """PUT /ucp/checkout-sessions/{id}"""

    line_items: Optional[list[LineItem]] = None
    fulfillment: Optional[Fulfillment] = None
    buyer: Optional[BuyerInfo] = None


class CompleteSessionRequest(BaseModel):
    """POST /ucp/checkout-sessions/{id}/complete"""

    payment_data: PaymentData
    fulfillment: Optional[Fulfillment] = None


# ---------------------------------------------------------------------------
# Error response (UCP spec format)
# ---------------------------------------------------------------------------

class UCPMessage(BaseModel):
    type: Literal["error", "warning", "info"] = "error"
    code: str = Field(..., examples=["OUT_OF_STOCK"])
    path: Optional[str] = Field(None, examples=["line_items[0].quantity"])
    content: str = Field(..., description="Human-readable message.")
    severity: Literal["recoverable", "fatal"] = "recoverable"


class UCPError(BaseModel):
    status: str
    messages: list[UCPMessage]


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Help type-checkers; at runtime we rebuild forward refs below.
    CheckoutSession.update_forward_refs()
    OrderRef.update_forward_refs()


# Rebuild forward references at runtime
CheckoutSession.model_rebuild()
OrderRef.model_rebuild()
