from pydantic import BaseModel, Field
from typing import Optional, Any, List, Union
from datetime import date as DateType, time, datetime
from uuid import UUID
from decimal import Decimal


class GymClassBase(BaseModel):
    title: Optional[str] = None
    theme_name: Optional[str] = None
    trainer_id: Optional[UUID] = None
    class_date: Optional[DateType] = None
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    max_bookings: Optional[int] = None
    max_waitings: Optional[int] = None
    booking_counts: Optional[int] = None
    attendance_count: Optional[int] = None
    booking_type: Optional[str] = None
    price: Optional[Decimal] = None
    gender: Optional[str] = None
    status: Optional[str] = None


class GymClassResponse(GymClassBase):
    id: UUID
    training_programme_id: Optional[int] = None
    terms_text: Optional[str] = None
    publish_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    schedule_id: Optional[int] = None
    layout_id: Optional[Union[int, UUID, str]] = None
    layouts: Optional[Any] = None
    fully_booked: bool = Field(
        default=False,
        description="No regular spots left (all layout seats booked, or booking_counts >= capacity).",
    )

    class Config:
        from_attributes = True


class ClassesListResponse(BaseModel):
    success: bool = True
    message: str = "Classes fetched successfully"
    data: List[GymClassResponse]
    count: int


# ---------------------------------------------------------------------------
# Class details (single class)
# ---------------------------------------------------------------------------

class ProgramShortResponse(BaseModel):
    id: int
    name: Optional[str] = None

    class Config:
        from_attributes = True


class TrainerShortResponse(BaseModel):
    id: str
    name: Optional[str] = None
    avatar: Optional[str] = None

    class Config:
        from_attributes = True


class LocationShortResponse(BaseModel):
    id: str
    name: Optional[str] = None

    class Config:
        from_attributes = True


class ScheduleShortResponse(BaseModel):
    date: Optional[DateType] = None
    start_time: Optional[time] = None
    end_time: Optional[time] = None

    class Config:
        from_attributes = True


class CapacityResponse(BaseModel):
    total: int = 0
    booked: int = 0
    waiting: int = 0
    max_waiting: int = 0
    available: int = 0

    class Config:
        from_attributes = True


class PricingResponse(BaseModel):
    drop_in_price: Optional[float] = None
    wallet_credits_required: Optional[int] = None
    currency: str = "QAR"

    class Config:
        from_attributes = True


class LayoutSeatResponse(BaseModel):
    id: str
    row: str
    col: int
    status: str
    type: str = "mat"
    booking_id: Optional[str] = None

    class Config:
        from_attributes = True


class LayoutResponse(BaseModel):
    rows: int
    columns: int
    seats: List[LayoutSeatResponse] = []

    class Config:
        from_attributes = True


class UserBookingResponse(BaseModel):
    has_booked: bool = False
    booking_id: Optional[str] = None
    seat_id: Optional[str] = None
    status: Optional[str] = None
    payment_mode: Optional[str] = None
    package_id: Optional[str] = None

    class Config:
        from_attributes = True


class ClassDetailsResponse(BaseModel):
    class_id: str
    name: Optional[str] = None
    booking_type: Optional[str] = None
    layout_id: Optional[Union[int, UUID, str]] = None
    layouts: Optional[Any] = None

    program: ProgramShortResponse
    trainer: TrainerShortResponse
    location: LocationShortResponse
    schedule: ScheduleShortResponse

    capacity: CapacityResponse
    pricing: PricingResponse
    layout: LayoutResponse
    user_booking: UserBookingResponse

    class Config:
        from_attributes = True


class ClassDetailsOuterResponse(BaseModel):
    success: bool = True
    message: str = "Class details fetched successfully"
    data: ClassDetailsResponse
