from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

PaymentMethod = Literal["free", "package", "wallet", "gateway"]


class BookingRequestBody(BaseModel):
    payment_method: PaymentMethod
    user_package_purchase_id: Optional[UUID] = Field(
        default=None,
        description="sales.id when payment_method is package",
    )
    seat_id: Optional[str] = Field(
        default=None,
        description='Layout seat label when gym_classes has layout configured (layout_id or layouts), e.g. "A1" (same id as class details layout.seats[].id)',
    )
    notes: Optional[str] = None


class BookingCancelRequestBody(BaseModel):
    reason: Optional[str] = None


class BookingValidateData(BaseModel):
    """
    checks: snake_case keys, each value is {"pass": bool, ...optional fields, "message" on failure}
    """

    valid: bool
    checks: Dict[str, Any]
    proceed_to: Optional[str] = None
    message: Optional[str] = None
    proposed_status: Optional[str] = None
    waiting_position: Optional[int] = None
    debug: Optional[Dict[str, Any]] = None


class BookingValidateResponse(BaseModel):
    success: bool = True
    message: str
    data: BookingValidateData


class BookingCreatedData(BaseModel):
    booking_id: UUID
    status: str
    waiting_position: Optional[int] = None
    payment_method: Optional[str] = None
    sessions_deducted: int = 0
    credits_deducted: Optional[Decimal] = None


class BookingCreateResponse(BaseModel):
    success: bool = True
    message: str
    data: BookingCreatedData


class BookingCancelledData(BaseModel):
    booking_id: UUID
    status: str
    cancelled_at: Optional[str] = None
    booking_counts: Optional[int] = None


class BookingCancelResponse(BaseModel):
    success: bool = True
    message: str
    data: BookingCancelledData


class MemberUpcomingBookingItem(BaseModel):
    booking_id: str
    order_id: Optional[str] = None
    class_id: str
    class_name: Optional[str] = None
    booking_type: Optional[str] = None
    status: str
    seat_id: Optional[str] = None
    date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    trainer: Optional[str] = None
    can_cancel: bool = False
    cancel_deadline: Optional[str] = None
    cancelled_at: Optional[str] = None


class MemberPastBookingItem(BaseModel):
    booking_id: str
    order_id: Optional[str] = None
    class_id: str
    class_name: Optional[str] = None
    booking_type: Optional[str] = None
    status: str
    seat_id: Optional[str] = None
    date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    trainer: Optional[str] = None
    can_cancel: bool = False
    cancel_deadline: Optional[str] = None
    cancelled_at: Optional[str] = None


class MemberWaitingBookingItem(BaseModel):
    booking_id: str
    order_id: Optional[str] = None
    class_name: Optional[str] = None
    status: str
    waiting_position: Optional[int] = None


class MemberBookingsResponse(BaseModel):
    upcoming: list[MemberUpcomingBookingItem] = Field(default_factory=list)
    past: list[MemberPastBookingItem] = Field(default_factory=list)
    waiting: list[MemberWaitingBookingItem] = Field(default_factory=list)
