"""
UCP ↔ WooCommerce adapter.

Translates UCP requests into WooCommerce DB operations and vice versa.
Keeps the route handlers thin — all business logic lives here.
"""
from __future__ import annotations

from fastapi import HTTPException, status

from db.woo_queries import (
    get_product,
    check_stock,
    get_shipping_options,
    create_order,
)
from models.ucp import (
    CheckoutSession,
    ItemRef,
    CreateSessionRequest,
    UpdateSessionRequest,
    CompleteSessionRequest,
    LineItem,
    Money,
    Totals,
    Fulfillment,
    ShippingOption,
    PaymentHandlerInfo,
    PaymentInfo,
    OrderRef,
    UCPError,
    UCPMessage,
)
from services.session_store import create_session, get_session, update_session
from services.stripe_service import charge_order
from config import get_settings

settings = get_settings()

TAX_RATE = 0.0825  # 8.25% — simplified flat tax for demo


def _price_to_micros(price_str) -> int:
    try:
        return int(round(float(price_str) * 1_000_000))
    except (TypeError, ValueError):
        return 0


async def create_checkout_session(req: CreateSessionRequest, db) -> CheckoutSession:
    """Validate items against WooCommerce DB and create a new session."""
    validated_items: list[LineItem] = []

    for i, li in enumerate(req.line_items):
        product = await get_product(db, li.item.id)
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=UCPError(
                    status="invalid_item",
                    messages=[UCPMessage(
                        type="error",
                        code="PRODUCT_NOT_FOUND",
                        path=f"line_items[{i}].item.id",
                        content=f"Product '{li.item.id}' not found.",
                        severity="fatal",
                    )],
                ).model_dump(),
            )

        in_stock = await check_stock(db, li.item.id, li.quantity)
        if not in_stock:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=UCPError(
                    status="out_of_stock",
                    messages=[UCPMessage(
                        type="error",
                        code="OUT_OF_STOCK",
                        path=f"line_items[{i}].quantity",
                        content=f"Product '{product['title']}' has insufficient stock.",
                        severity="recoverable",
                    )],
                ).model_dump(),
            )

        # Use actual WooCommerce price (overrides any client-supplied price)
        price_micros = _price_to_micros(product["price"])
        total_micros = price_micros * li.quantity
        validated_items.append(
            LineItem(
                item=ItemRef(id=str(product["id"]), title=product["title"]),
                quantity=li.quantity,
                price_per_item=Money(amount_micros=price_micros),
                total_price=Money(amount_micros=total_micros),
            )
        )

    totals = _calculate_totals(validated_items)

    session = CheckoutSession(
        id="",
        line_items=validated_items,
        totals=totals,
        buyer=req.buyer,
        payment=PaymentInfo(handlers=[PaymentHandlerInfo(name="google_pay")]),
        privacy_policy=f"{settings.wc_domain}/privacy",
        terms_of_service=f"{settings.wc_domain}/terms",
    )
    return await create_session(session, db)


async def update_checkout_session(
    session_id: str,
    req: UpdateSessionRequest,
    db,
) -> CheckoutSession:
    session = await _require_session(session_id, db)

    if req.line_items is not None:
        session.line_items = req.line_items

    if req.buyer is not None:
        session.buyer = req.buyer

    if req.fulfillment is not None:
        address = req.fulfillment.address or (session.fulfillment.address if session.fulfillment else None)
        country = address.country_code if address else "US"

        raw_options = await get_shipping_options(db, country)
        subtotal_micros = sum(
            (li.total_price.amount_micros if li.total_price else li.price_per_item.amount_micros * li.quantity)
            for li in session.line_items
        )
        FREE_SHIPPING_THRESHOLD_MICROS = 50_000_000  # $50.00
        shipping_options = [
            ShippingOption(
                id=o["id"],
                label=o["label"],
                price=Money(amount_micros=o["price_micros"]),
            )
            for o in raw_options
            if o["price_micros"] > 0 or subtotal_micros >= FREE_SHIPPING_THRESHOLD_MICROS
        ]

        selected_id = req.fulfillment.selected_option_id
        if selected_id:
            option_ids = {o.id for o in shipping_options}
            if selected_id not in option_ids:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=UCPError(
                        status="invalid_shipping",
                        messages=[UCPMessage(
                            type="error",
                            code="INVALID_SHIPPING_OPTION",
                            path="fulfillment.selected_option_id",
                            content=f"Shipping option '{selected_id}' is not available.",
                            severity="recoverable",
                        )],
                    ).model_dump(),
                )

        session.fulfillment = Fulfillment(
            address=address,
            selected_option_id=selected_id,
            options=shipping_options,
        )

    session.totals = _calculate_totals(session.line_items, session.fulfillment)
    return await update_session(session, db)


async def complete_checkout_session(
    session_id: str,
    req: CompleteSessionRequest,
    db,
) -> CheckoutSession:
    session = await _require_session(session_id, db)

    if session.status != "incomplete":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=UCPError(
                status="invalid_state",
                messages=[UCPMessage(
                    type="error",
                    code="INVALID_SESSION_STATE",
                    content=f"Session is '{session.status}', expected 'incomplete'.",
                    severity="fatal",
                )],
            ).model_dump(),
        )

    # Determine shipping/billing addresses
    fulfillment = req.fulfillment or session.fulfillment
    billing = req.payment_data.billing_address
    shipping = fulfillment.address if fulfillment else None

    billing_dict = billing.model_dump() if billing else {}
    shipping_dict = shipping.model_dump() if shipping else billing_dict

    totals = _calculate_totals(session.line_items, fulfillment)
    line_item_dicts = [
        {
            "product_id": li.item.id,
            "title": li.item.title,
            "quantity": li.quantity,
            "subtotal": li.price_per_item.amount_micros / 1_000_000,
            "total": li.total_price.amount_micros / 1_000_000 if li.total_price else 0,
        }
        for li in session.line_items
    ]

    # Charge via Stripe (no-op if STRIPE_SECRET_KEY not set)
    stripe_result = await charge_order(totals.total.amount_micros)

    order = await create_order(
        db,
        session_id=session_id,
        line_items=line_item_dicts,
        billing=billing_dict,
        shipping=shipping_dict,
        totals={
            "currency_code": "USD",
            "tax_micros": totals.tax.amount_micros,
            "total_micros": totals.total.amount_micros,
        },
        buyer_email=session.buyer.email if session.buyer else "",
        payment_method="stripe",
        transaction_id=stripe_result["transaction_id"],
    )

    session.status = "completed"
    session.totals = totals
    session.order = OrderRef(
        id=str(order["order_id"]),
        status=order.get("status"),
        view_url=f"{settings.wp_domain}/wp-admin/admin.php?page=wc-orders&action=edit&id={order['order_id']}",
    )
    return await update_session(session, db)


async def cancel_checkout_session(session_id: str, db=None) -> CheckoutSession:
    session = await _require_session(session_id, db)
    if session.status == "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=UCPError(
                status="invalid_state",
                messages=[UCPMessage(
                    type="error",
                    code="CANNOT_CANCEL_COMPLETED",
                    content="A completed session cannot be cancelled.",
                    severity="fatal",
                )],
            ).model_dump(),
        )
    session.status = "cancelled"
    return await update_session(session, db)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _require_session(session_id: str, db=None) -> CheckoutSession:
    session = await get_session(session_id, db)
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


def _calculate_totals(line_items: list[LineItem], fulfillment=None) -> Totals:
    subtotal_micros = sum(
        (li.total_price.amount_micros if li.total_price else li.price_per_item.amount_micros * li.quantity)
        for li in line_items
    )
    tax_micros = int(subtotal_micros * TAX_RATE)

    shipping_micros = 0
    if fulfillment and fulfillment.selected_option_id and fulfillment.options:
        for opt in fulfillment.options:
            if opt.id == fulfillment.selected_option_id:
                shipping_micros = opt.price.amount_micros
                break

    total_micros = subtotal_micros + tax_micros + shipping_micros

    return Totals(
        subtotal=Money(amount_micros=subtotal_micros),
        tax=Money(amount_micros=tax_micros),
        shipping=Money(amount_micros=shipping_micros),
        total=Money(amount_micros=total_micros),
    )
