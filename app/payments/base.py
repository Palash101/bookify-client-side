"""
Base Payment Gateway - Abstract base class for all payment providers.
All payment gateway implementations must inherit from this class.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & Data Structures
# ---------------------------------------------------------------------------

class PaymentStatus(str, Enum):
    PENDING    = "pending"
    SUCCESS    = "success"
    FAILED     = "failed"
    CANCELLED  = "cancelled"
    REFUNDED   = "refunded"


class GatewayType(str, Enum):
    STRIPE      = "stripe"
    PAYPAL      = "paypal"
    MYFATOORAH  = "myfatoorah"


@dataclass
class PaymentRequest:
    """Normalized payment request passed into every gateway."""
    amount: float                        # Amount in major currency unit (e.g. 10.00)
    currency: str                        # ISO 4217 code, e.g. "USD", "KWD"
    order_id: str                        # Your internal order / invoice ID
    customer_email: str
    customer_name: str
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaymentResponse:
    """Normalized response returned by every gateway."""
    success: bool
    gateway: GatewayType
    payment_url: Optional[str] = None    # Redirect URL for hosted pages
    transaction_id: Optional[str] = None # Gateway-side transaction/session ID
    status: PaymentStatus = PaymentStatus.PENDING
    raw_response: dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None


@dataclass
class CallbackResult:
    """Normalized result from a payment callback / webhook."""
    success: bool
    order_id: str
    transaction_id: Optional[str]
    status: PaymentStatus
    gateway: GatewayType
    amount: Optional[float] = None
    currency: Optional[str] = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None


@dataclass
class RefundResponse:
    success: bool
    refund_id: Optional[str]
    transaction_id: str
    amount: float
    gateway: GatewayType
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Base Gateway
# ---------------------------------------------------------------------------

class BasePaymentGateway(ABC):
    """
    Abstract base class for payment gateways.

    Every concrete gateway (Stripe, PayPal, MyFatoorah, …) must:
      1. Accept its own settings dict in __init__ and call super().__init__().
      2. Implement all abstract methods below.
      3. NOT raise unhandled exceptions — catch provider errors and return
         appropriate response objects.
    """

    # Subclasses set this so logs & error messages are self-identifying.
    GATEWAY_TYPE: GatewayType

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self._validate_settings()
        logger.info("Initialized payment gateway: %s", self.GATEWAY_TYPE)

    # ------------------------------------------------------------------
    # Abstract interface — every gateway MUST implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def _validate_settings(self) -> None:
        """
        Validate that all required keys are present in self.settings.
        Raise ValueError with a clear message if anything is missing.
        """

    @abstractmethod
    def create_payment(self, request: PaymentRequest) -> PaymentResponse:
        """
        Create a payment session / intent and return a hosted payment URL.

        Args:
            request: Normalized PaymentRequest.

        Returns:
            PaymentResponse with payment_url populated on success.
        """

    @abstractmethod
    def handle_callback(self, payload: dict[str, Any]) -> CallbackResult:
        """
        Process an incoming webhook / redirect callback from the gateway.

        Args:
            payload: Raw query-params or JSON body from the provider.

        Returns:
            CallbackResult with the normalized outcome.
        """

    @abstractmethod
    def verify_payment(self, transaction_id: str) -> CallbackResult:
        """
        Actively query the gateway to verify a payment's current status.
        Useful for reconciliation or when a webhook is missed.

        Args:
            transaction_id: The gateway-side transaction/session ID.

        Returns:
            CallbackResult with the current status.
        """

    @abstractmethod
    def refund_payment(
        self,
        transaction_id: str,
        amount: float,
        reason: str = "",
    ) -> RefundResponse:
        """
        Issue a full or partial refund.

        Args:
            transaction_id: Gateway-side transaction ID to refund.
            amount: Amount to refund (must be ≤ original charge).
            reason: Human-readable refund reason.

        Returns:
            RefundResponse.
        """

    # ------------------------------------------------------------------
    # Shared helpers available to all subclasses
    # ------------------------------------------------------------------

    def get_callback_url(self, base_url: str, path: str = "/payment/callback") -> str:
        """Build the full callback URL from configured base URL."""
        base = self.settings.get("callback_base_url", base_url).rstrip("/")
        return f"{base}{path}/{self.GATEWAY_TYPE.value}"

    def get_success_url(self, base_url: str) -> str:
        return self.settings.get(
            "success_url",
            f"{base_url.rstrip('/')}/payment/success",
        )

    def get_cancel_url(self, base_url: str) -> str:
        return self.settings.get(
            "cancel_url",
            f"{base_url.rstrip('/')}/payment/cancel",
        )

    def _log_error(self, message: str, exc: Optional[Exception] = None) -> None:
        logger.error("[%s] %s — %s", self.GATEWAY_TYPE, message, exc)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} gateway={self.GATEWAY_TYPE}>"