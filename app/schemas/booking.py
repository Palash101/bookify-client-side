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
        description='Layout seat label when gym_classes.layout_id is set, e.g. "A1" (same id as class details layout.seats[].id)',
    )
    notes: Optional[str] = None


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
