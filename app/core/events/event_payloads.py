"""Per-event Pub/Sub ``data`` payloads — reusable across producers."""
from __future__ import annotations

from app.models.class_booking import ClassBooking


def build_wallet_notification_data(*, wallet_transaction_id: str) -> dict[str, str]:
    """Minimal ``data`` for wallet events — consumer resolves user/email from DB."""
    return {"wallet_transaction_id": str(wallet_transaction_id)}


def build_package_notification_data(*, user_package_id: str) -> dict[str, str]:
    return {"user_package_id": str(user_package_id)}


def build_payment_notification_data(*, sales_transaction_id: str) -> dict[str, str]:
    return {"sales_transaction_id": str(sales_transaction_id)}


def build_booking_notification_data(booking: ClassBooking) -> dict[str, str]:
    """Minimal ``data`` for booking events — consumer resolves user/email from DB."""
    return {"booking_id": str(booking.id)}
