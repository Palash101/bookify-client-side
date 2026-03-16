"""
MyFatoorah Payment Gateway Implementation.
Supports Kuwait, Saudi Arabia, UAE, Bahrain, Qatar, Oman, Jordan, Egypt.

Required settings keys:
  - api_key           : MyFatoorah API token
  - callback_base_url : Base URL for your server
  - mode              : "live" | "test"  (default: "test")
  - country_iso       : ISO-3-letter country code (default: "KWT")
  - success_url       : (optional)
  - cancel_url        : (optional)
"""

from typing import Any, Optional
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

MYFATOORAH_BASE_URLS = {
    "live": "https://api.myfatoorah.com",
    "test": "https://apitest.myfatoorah.com",
}


class MyFatoorahPaymentGateway(BasePaymentGateway):

    GATEWAY_TYPE = GatewayType.MYFATOORAH

    # ------------------------------------------------------------------
    # Init / Validation
    # ------------------------------------------------------------------

    def _validate_settings(self) -> None:
        required = ["api_key", "callback_base_url"]
        missing = [k for k in required if not self.settings.get(k)]
        if missing:
            raise ValueError(f"MyFatoorah gateway missing settings: {missing}")

        self._mode = self.settings.get("mode", "test")
        self._base_url = MYFATOORAH_BASE_URLS.get(self._mode, MYFATOORAH_BASE_URLS["test"])
        self._country_iso = self.settings.get("country_iso", "KWT")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.settings['api_key']}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        resp = httpx.post(
            f"{self._base_url}{path}",
            json=payload,
            headers=self._headers(),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = httpx.get(
            f"{self._base_url}{path}",
            params=params,
            headers=self._headers(),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _map_status(mf_status: str) -> PaymentStatus:
        status_map = {
            "Paid":           PaymentStatus.SUCCESS,
            "DeniedByRisk":   PaymentStatus.FAILED,
            "Failed":         PaymentStatus.FAILED,
            "Expired":        PaymentStatus.FAILED,
            "Cancelled":      PaymentStatus.CANCELLED,
            "Pending":        PaymentStatus.PENDING,
            "InProgress":     PaymentStatus.PENDING,
            "Authorized":     PaymentStatus.PENDING,
            "Refunded":       PaymentStatus.REFUNDED,
            "PartialRefund":  PaymentStatus.REFUNDED,
        }
        return status_map.get(mf_status, PaymentStatus.PENDING)

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    def create_payment(self, request: PaymentRequest) -> PaymentResponse:
        try:
            payload = {
                "CustomerName":          request.customer_name,
                "NotificationOption":    "LNK",
                "InvoiceValue":          request.amount,
                "DisplayCurrencyIso":    request.currency.upper(),
                "MobileCountryCode":     self.settings.get("mobile_country_code", "+965"),
                "CustomerEmail":         request.customer_email,
                "CallBackUrl":           self.get_callback_url(self.settings["callback_base_url"]),
                "ErrorUrl":              self.get_cancel_url(self.settings["callback_base_url"]),
                "Language":              self.settings.get("language", "en"),
                "CustomerReference":     request.order_id,
                "InvoiceItems": [
                    {
                        "ItemName":     request.description or f"Order {request.order_id}",
                        "Quantity":     1,
                        "UnitPrice":    request.amount,
                    }
                ],
            }

            data = self._post("/v2/SendPayment", payload)

            if not data.get("IsSuccess"):
                return PaymentResponse(
                    success=False,
                    gateway=self.GATEWAY_TYPE,
                    error_message=data.get("Message", "Unknown error from MyFatoorah"),
                    raw_response=data,
                )

            invoice_data = data["Data"]
            return PaymentResponse(
                success=True,
                gateway=self.GATEWAY_TYPE,
                payment_url=invoice_data["InvoiceURL"],
                transaction_id=str(invoice_data["InvoiceId"]),
                status=PaymentStatus.PENDING,
                raw_response=invoice_data,
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
        MyFatoorah redirects to CallBackUrl with query param: paymentId
        payload should be: { "paymentId": "<payment_id>" }
        """
        payment_id = payload.get("paymentId")
        if not payment_id:
            return CallbackResult(
                success=False,
                order_id="",
                transaction_id=None,
                status=PaymentStatus.FAILED,
                gateway=self.GATEWAY_TYPE,
                error_message="Missing paymentId in callback",
            )

        try:
            data = self._post("/v2/GetPaymentStatus", {"Key": payment_id, "KeyType": "PaymentId"})

            if not data.get("IsSuccess"):
                return CallbackResult(
                    success=False,
                    order_id="",
                    transaction_id=payment_id,
                    status=PaymentStatus.FAILED,
                    gateway=self.GATEWAY_TYPE,
                    error_message=data.get("Message"),
                    raw_payload=data,
                )

            inv = data["Data"]
            status = self._map_status(inv.get("InvoiceStatus", ""))

            return CallbackResult(
                success=status == PaymentStatus.SUCCESS,
                order_id=inv.get("CustomerReference", ""),
                transaction_id=str(inv.get("InvoiceId", payment_id)),
                status=status,
                gateway=self.GATEWAY_TYPE,
                amount=inv.get("InvoiceValue"),
                currency=inv.get("CurrencyIso"),
                raw_payload=inv,
            )
        except Exception as exc:
            self._log_error("handle_callback failed", exc)
            return CallbackResult(
                success=False,
                order_id="",
                transaction_id=payment_id,
                status=PaymentStatus.FAILED,
                gateway=self.GATEWAY_TYPE,
                error_message=str(exc),
            )

    def verify_payment(self, transaction_id: str) -> CallbackResult:
        try:
            data = self._post(
                "/v2/GetPaymentStatus",
                {"Key": transaction_id, "KeyType": "InvoiceId"},
            )

            if not data.get("IsSuccess"):
                return CallbackResult(
                    success=False,
                    order_id="",
                    transaction_id=transaction_id,
                    status=PaymentStatus.FAILED,
                    gateway=self.GATEWAY_TYPE,
                    error_message=data.get("Message"),
                )

            inv = data["Data"]
            status = self._map_status(inv.get("InvoiceStatus", ""))

            return CallbackResult(
                success=True,
                order_id=inv.get("CustomerReference", ""),
                transaction_id=str(inv.get("InvoiceId", transaction_id)),
                status=status,
                gateway=self.GATEWAY_TYPE,
                amount=inv.get("InvoiceValue"),
                currency=inv.get("CurrencyIso"),
                raw_payload=inv,
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
        try:
            payload = {
                "KeyType":          "InvoiceId",
                "Key":              transaction_id,
                "RefundChargeOnCustomer": False,
                "ServiceCharge":    0,
                "Amount":           amount,
                "Comment":          reason or "Refund",
            }
            data = self._post("/v2/MakeRefund", payload)

            if not data.get("IsSuccess"):
                return RefundResponse(
                    success=False,
                    refund_id=None,
                    transaction_id=transaction_id,
                    amount=amount,
                    gateway=self.GATEWAY_TYPE,
                    error_message=data.get("Message"),
                )

            refund_data = data.get("Data", {})
            return RefundResponse(
                success=True,
                refund_id=str(refund_data.get("RefundId", "")),
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