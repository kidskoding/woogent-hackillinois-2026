"""
Stripe payment processing for UCP checkout.

Uses a test PaymentMethod (pm_card_visa) so the entire checkout flow
works end-to-end in Stripe test mode without a real card.

Set STRIPE_SECRET_KEY=sk_test_... in .env to enable.
If the key is absent the charge is skipped and an empty transaction_id
is returned so the rest of the checkout still completes.
"""
from __future__ import annotations

import asyncio
import stripe

from config import get_settings

settings = get_settings()


async def charge_order(amount_micros: int, currency: str = "USD") -> dict:
    """
    Create and immediately confirm a Stripe PaymentIntent.

    Returns:
        {
            "payment_intent_id": "pi_...",
            "transaction_id":    "ch_..."   ← stored on the WC order
        }
    """
    if not settings.stripe_test_key:
        return {"payment_intent_id": "", "transaction_id": ""}

    def _create() -> stripe.PaymentIntent:
        stripe.api_key = settings.stripe_test_key
        return stripe.PaymentIntent.create(
            amount=round(amount_micros / 10_000),  # micros → cents
            currency=currency.lower(),
            payment_method="pm_card_visa",
            confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
        )

    intent = await asyncio.to_thread(_create)
    return {
        "payment_intent_id": intent["id"],
        # latest_charge is the charge ID; fall back to intent ID if not present
        "transaction_id": intent.get("latest_charge") or intent["id"],
    }
