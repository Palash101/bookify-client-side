from pydantic import BaseModel
from typing import Optional, Any, List
from datetime import date, time, datetime
from uuid import UUID
from decimal import Decimal


class GymClassBase(BaseModel):
    title: Optional[str] = None
    theme_name: Optional[str] = None
    trainer_id: Optional[UUID] = None
    class_date: Optional[date] = None
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
    layout: Optional[Any] = None

    class Config:
        from_attributes = True


class ClassesListResponse(BaseModel):
    success: bool = True
    message: str = "Classes fetched successfully"
    data: List[GymClassResponse]
    count: int
