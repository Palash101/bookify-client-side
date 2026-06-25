"""Per-event Pub/Sub ``data`` payloads — reusable across producers."""
from __future__ import annotations

from typing import Any

from app.core.events.event_types import (
    CLIENT_BOOKING_CANCELLED,
    CLIENT_BOOKING_CONFIRMED,
    CLIENT_BOOKING_CREATED,
    CLIENT_BOOKING_PENDING_PAYMENT,
    CLIENT_BOOKING_WAITLIST_JOINED,
    CLIENT_BOOKING_WAITLIST_PROMOTED,
)
from app.models.class_booking import ClassBooking

# user_id 0cb16132-c289-49ee-bfc4-000e89f417a4 — latest booking (ORG-102)
DEFAULT_WAITLIST_JOINED_EVENT_DATA: dict[str, str] = {
    "booking_id": "9e1ba01d-91d0-42bc-9db6-18ea3171373d",
}

DEFAULT_BOOKING_CREATED_EVENT_DATA: dict[str, str] = {
    "booking_id": "ed9dbd32-fe50-4531-91de-e7cccc7287dd",
}

# user_id 0cb16132-c289-49ee-bfc4-000e89f417a4 — confirmed booking (ORG-102)
DEFAULT_BOOKING_CONFIRMED_EVENT_DATA: dict[str, str] = {
    "booking_id": "9e1ba01d-91d0-42bc-9db6-18ea3171373d",
}

DEFAULT_BOOKING_CANCELLED_EVENT_DATA: dict[str, str] = {
    "booking_id": "9e1ba01d-91d0-42bc-9db6-18ea3171373d",
}

DEFAULT_BOOKING_PENDING_PAYMENT_EVENT_DATA: dict[str, str] = {
    "booking_id": "9e1ba01d-91d0-42bc-9db6-18ea3171373d",
}

# user_id 0cb16132-c289-49ee-bfc4-000e89f417a4
DEFAULT_WALLET_TOPUP_SUCCESS_EVENT_DATA: dict[str, str] = {
    "user_id": "0cb16132-c289-49ee-bfc4-000e89f417a4",
}

DEFAULT_WALLET_TOPUP_FAILED_EVENT_DATA: dict[str, str] = {
    "user_id": "0cb16132-c289-49ee-bfc4-000e89f417a4",
}

DEFAULT_WALLET_DEBITED_EVENT_DATA: dict[str, str] = {
    "user_id": "0cb16132-c289-49ee-bfc4-000e89f417a4",
}


DEFAULT_PACKAGE_PURCHASED_EVENT_DATA: dict[str, str] = {
    "package_id": "00000000-0000-0000-0000-000000000001",
}

DEFAULT_PACKAGE_PURCHASE_FAILED_EVENT_DATA: dict[str, str] = {
    "package_id": "00000000-0000-0000-0000-000000000001",
}

DEFAULT_PAYMENT_FAILED_EVENT_DATA: dict[str, str] = {
    "order_id": "ed9dbd32-fe50-4531-91de-e7cccc7287dd",
}


def build_wallet_notification_data(*, user_id: str) -> dict[str, str]:
    return {"user_id": str(user_id)}


def build_package_notification_data(*, package_id: str) -> dict[str, str]:
    return {"package_id": str(package_id)}


def build_payment_notification_data(*, order_id: str) -> dict[str, str]:
    return {"order_id": str(order_id)}

_BOOKING_EMAIL_EVENTS = (
    CLIENT_BOOKING_CREATED,
    CLIENT_BOOKING_CONFIRMED,
    CLIENT_BOOKING_WAITLIST_JOINED,
    CLIENT_BOOKING_WAITLIST_PROMOTED,
    CLIENT_BOOKING_PENDING_PAYMENT,
    CLIENT_BOOKING_CANCELLED,
)


def build_booking_notification_data(
    booking: ClassBooking,
    event_type: str,
) -> dict[str, Any]:
    """Build minimal ``data`` for booking email events — always ``booking_id`` only."""
    if event_type in _BOOKING_EMAIL_EVENTS:
        return {"booking_id": str(booking.id)}
    return {"booking_id": str(booking.id)}
