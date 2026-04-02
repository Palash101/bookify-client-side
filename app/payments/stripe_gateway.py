"""
Stripe Payment Gateway Implementation.

Required settings keys:
  - secret_key        : Stripe secret key  (sk_live_… / sk_test_…)
  - webhook_secret    : Stripe webhook signing secret
  - callback_base_url : Base URL for your server (e.g. https://api.myapp.com)
  - success_url       : (optional) redirect after successful payment
  - cancel_url        : (optional) redirect after cancelled payment
"""

from typing import Any
import logging
from uuid import uuid4

try:
    import stripe as stripe_lib
except ImportError:
    stripe_lib = None  # handled at runtime

from .base import (
    BasePaymentGateway,
    CallbackResult,
    GatewayType,
    PaymentRequest,
    PaymentResponse,
    PaymentStatus,
    RefundResponse,
)

logger = logging.getLogger(__name__)


class StripePaymentGateway(BasePaymentGateway):

    GATEWAY_TYPE = GatewayType.STRIPE

    # ------------------------------------------------------------------
    # Init / Validation
    # ------------------------------------------------------------------

    def _validate_settings(self) -> None:
        required = ["secret_key", "webhook_secret", "callback_base_url"]
        missing = [k for k in required if not self.settings.get(k)]
        if missing:
            raise ValueError(f"Stripe gateway missing settings: {missing}")

        if stripe_lib is None:
            raise ImportError("stripe package is not installed. Run: pip install stripe")

        stripe_lib.api_key = self.settings["secret_key"]

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    def create_payment(self, request: PaymentRequest) -> PaymentResponse:
        try:
            session = stripe_lib.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[
                    {
                        "price_data": {
                            "currency": request.currency.lower(),
                            "unit_amount": int(request.amount * 100),  # cents
                            "product_data": {
                                "name": request.description or f"Order {request.order_id}",
                            },
                        },
                        "quantity": 1,
                    }
                ],
                mode="payment",
                customer_email=request.customer_email,
                client_reference_id=request.order_id,
                metadata={"order_id": request.order_id, **request.metadata},
                success_url=self.get_success_url(self.settings["callback_base_url"])
                            + "?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=self.get_cancel_url(self.settings["callback_base_url"]),
            )
            return PaymentResponse(
                success=True,
                gateway=self.GATEWAY_TYPE,
                payment_url=getattr(session, "url", None) or (session.get("url") if isinstance(session, dict) else None),
                transaction_id=getattr(session, "id", None) or (session.get("id") if isinstance(session, dict) else None),
                status=PaymentStatus.PENDING,
                raw_response=dict(session) if isinstance(session, dict) else getattr(session, "to_dict", lambda: {})(),
            )
        except stripe_lib.error.StripeError as exc:
            self._log_error("create_payment failed", exc)
            return PaymentResponse(
                success=False,
                gateway=self.GATEWAY_TYPE,
                error_message=str(exc),
            )
        except Exception as exc:
            error_id = str(uuid4())
            logger.exception("[stripe] create_payment unexpected error (error_id=%s)", error_id)
            return PaymentResponse(
                success=False,
                gateway=self.GATEWAY_TYPE,
                error_message=f"Stripe create_payment error (error_id={error_id}): {type(exc).__name__}: {exc}",
            )

    def handle_callback(self, payload: dict[str, Any]) -> CallbackResult:
        """
        Processes a Stripe webhook event.
        payload should contain: { "raw_body": bytes, "stripe_signature": str }
        """
        try:
            event = stripe_lib.Webhook.construct_event(
                payload["raw_body"],
                payload["stripe_signature"],
                self.settings["webhook_secret"],
            )
        except (stripe_lib.error.SignatureVerificationError, KeyError) as exc:
            self._log_error("Webhook signature verification failed", exc)
            return CallbackResult(
                success=False,
                order_id="",
                transaction_id=None,
                status=PaymentStatus.FAILED,
                gateway=self.GATEWAY_TYPE,
                error_message=str(exc),
            )

        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            return CallbackResult(
                success=True,
                order_id=session.get("client_reference_id", ""),
                transaction_id=session["id"],
                status=PaymentStatus.SUCCESS,
                gateway=self.GATEWAY_TYPE,
                amount=session["amount_total"] / 100,
                currency=session["currency"].upper(),
                raw_payload=dict(session),
            )

        # Unhandled event type — not an error, just not actionable
        return CallbackResult(
            success=True,
            order_id="",
            transaction_id=None,
            status=PaymentStatus.PENDING,
            gateway=self.GATEWAY_TYPE,
            raw_payload=payload,
        )

    def verify_payment(self, transaction_id: str) -> CallbackResult:
        try:
            session = stripe_lib.checkout.Session.retrieve(transaction_id)
            status = (
                PaymentStatus.SUCCESS
                if session["payment_status"] == "paid"
                else PaymentStatus.PENDING
            )
            return CallbackResult(
                success=True,
                order_id=session.get("client_reference_id", ""),
                transaction_id=session["id"],
                status=status,
                gateway=self.GATEWAY_TYPE,
                amount=session["amount_total"] / 100 if session.get("amount_total") else None,
                currency=session.get("currency", "").upper() or None,
                raw_payload=dict(session),
            )
        except stripe_lib.error.StripeError as exc:
            self._log_error("verify_payment failed", exc)
            return CallbackResult(
                success=False,
                order_id="",
                transaction_id=transaction_id,
                status=PaymentStatus.FAILED,
                gateway=self.GATEWAY_TYPE,
                error_message=str(exc),
            )

    def refund_payment(
        self,
        transaction_id: str,
        amount: float,
        reason: str = "",
    ) -> RefundResponse:
        try:
            # Retrieve the payment intent from the session first
            session = stripe_lib.checkout.Session.retrieve(transaction_id)
            payment_intent_id = session.get("payment_intent")

            refund = stripe_lib.Refund.create(
                payment_intent=payment_intent_id,
                amount=int(amount * 100),
                reason=reason or None,
            )
            return RefundResponse(
                success=True,
                refund_id=refund["id"],
                transaction_id=transaction_id,
                amount=refund["amount"] / 100,
                gateway=self.GATEWAY_TYPE,
            )
        except stripe_lib.error.StripeError as exc:
            self._log_error("refund_payment failed", exc)
            return RefundResponse(
                success=False,
                refund_id=None,
                transaction_id=transaction_id,
                amount=amount,
                gateway=self.GATEWAY_TYPE,
                error_message=str(exc),
            )