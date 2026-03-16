"""
PayPal Payment Gateway Implementation (REST Orders API v2).

Required settings keys:
  - client_id         : PayPal App client ID
  - client_secret     : PayPal App client secret
  - callback_base_url : Base URL for your server
  - mode              : "sandbox" | "live"  (default: "sandbox")
  - success_url       : (optional)
  - cancel_url        : (optional)
"""

from typing import Any
import logging

import httpx

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

PAYPAL_BASE_URLS = {
    "live":    "https://api-m.paypal.com",
    "sandbox": "https://api-m.sandbox.paypal.com",
}


class PayPalPaymentGateway(BasePaymentGateway):

    GATEWAY_TYPE = GatewayType.PAYPAL

    # ------------------------------------------------------------------
    # Init / Validation
    # ------------------------------------------------------------------

    def _validate_settings(self) -> None:
        required = ["client_id", "client_secret", "callback_base_url"]
        missing = [k for k in required if not self.settings.get(k)]
        if missing:
            raise ValueError(f"PayPal gateway missing settings: {missing}")

        self._mode = self.settings.get("mode", "sandbox")
        self._base_url = PAYPAL_BASE_URLS.get(self._mode, PAYPAL_BASE_URLS["sandbox"])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        """Fetch a short-lived OAuth2 bearer token from PayPal."""
        resp = httpx.post(
            f"{self._base_url}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(self.settings["client_id"], self.settings["client_secret"]),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _map_status(paypal_status: str) -> PaymentStatus:
        mapping = {
            "COMPLETED": PaymentStatus.SUCCESS,
            "APPROVED":  PaymentStatus.PENDING,
            "CREATED":   PaymentStatus.PENDING,
            "VOIDED":    PaymentStatus.CANCELLED,
        }
        return mapping.get(paypal_status.upper(), PaymentStatus.PENDING)

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    def create_payment(self, request: PaymentRequest) -> PaymentResponse:
        try:
            payload = {
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "reference_id": request.order_id,
                        "description": request.description or f"Order {request.order_id}",
                        "amount": {
                            "currency_code": request.currency.upper(),
                            "value": f"{request.amount:.2f}",
                        },
                    }
                ],
                "application_context": {
                    "return_url": self.get_success_url(self.settings["callback_base_url"]),
                    "cancel_url": self.get_cancel_url(self.settings["callback_base_url"]),
                    "brand_name": self.settings.get("brand_name", "My Store"),
                    "user_action": "PAY_NOW",
                },
            }

            resp = httpx.post(
                f"{self._base_url}/v2/checkout/orders",
                json=payload,
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            approval_url = next(
                (link["href"] for link in data.get("links", []) if link["rel"] == "approve"),
                None,
            )

            return PaymentResponse(
                success=True,
                gateway=self.GATEWAY_TYPE,
                payment_url=approval_url,
                transaction_id=data["id"],
                status=PaymentStatus.PENDING,
                raw_response=data,
            )
        except Exception as exc:
            self._log_error("create_payment failed", exc)
            return PaymentResponse(
                success=False,
                gateway=self.GATEWAY_TYPE,
                error_message=str(exc),
            )

    def handle_callback(self, payload: dict[str, Any]) -> CallbackResult:
        """
        Called with PayPal redirect query params:
          { "token": "<order_id>", "PayerID": "<payer_id>" }
        Captures the payment automatically.
        """
        order_id = payload.get("token")
        if not order_id:
            return CallbackResult(
                success=False,
                order_id="",
                transaction_id=None,
                status=PaymentStatus.FAILED,
                gateway=self.GATEWAY_TYPE,
                error_message="Missing token in callback payload",
            )

        try:
            resp = httpx.post(
                f"{self._base_url}/v2/checkout/orders/{order_id}/capture",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            purchase_unit = (data.get("purchase_units") or [{}])[0]
            capture = (
                purchase_unit.get("payments", {}).get("captures") or [{}]
            )[0]

            return CallbackResult(
                success=data["status"] == "COMPLETED",
                order_id=purchase_unit.get("reference_id", ""),
                transaction_id=capture.get("id"),
                status=self._map_status(data["status"]),
                gateway=self.GATEWAY_TYPE,
                amount=float(capture.get("amount", {}).get("value", 0)),
                currency=capture.get("amount", {}).get("currency_code"),
                raw_payload=data,
            )
        except Exception as exc:
            self._log_error("handle_callback failed", exc)
            return CallbackResult(
                success=False,
                order_id="",
                transaction_id=None,
                status=PaymentStatus.FAILED,
                gateway=self.GATEWAY_TYPE,
                error_message=str(exc),
            )

    def verify_payment(self, transaction_id: str) -> CallbackResult:
        try:
            resp = httpx.get(
                f"{self._base_url}/v2/checkout/orders/{transaction_id}",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            purchase_unit = (data.get("purchase_units") or [{}])[0]
            amount_obj = purchase_unit.get("amount", {})

            return CallbackResult(
                success=True,
                order_id=purchase_unit.get("reference_id", ""),
                transaction_id=data["id"],
                status=self._map_status(data.get("status", "")),
                gateway=self.GATEWAY_TYPE,
                amount=float(amount_obj.get("value", 0)) if amount_obj else None,
                currency=amount_obj.get("currency_code"),
                raw_payload=data,
            )
        except Exception as exc:
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
        """transaction_id here is the *capture* ID, not the order ID."""
        try:
            payload: dict[str, Any] = {
                "amount": {"value": f"{amount:.2f}", "currency_code": "USD"},
            }
            if reason:
                payload["note_to_payer"] = reason

            resp = httpx.post(
                f"{self._base_url}/v2/payments/captures/{transaction_id}/refund",
                json=payload,
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            return RefundResponse(
                success=data.get("status") == "COMPLETED",
                refund_id=data.get("id"),
                transaction_id=transaction_id,
                amount=amount,
                gateway=self.GATEWAY_TYPE,
            )
        except Exception as exc:
            self._log_error("refund_payment failed", exc)
            return RefundResponse(
                success=False,
                refund_id=None,
                transaction_id=transaction_id,
                amount=amount,
                gateway=self.GATEWAY_TYPE,
                error_message=str(exc),
            )