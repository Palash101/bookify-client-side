from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel

from app.models.event_event import EventStatus, EventType
from app.schemas.transactions import PaginationMeta


class EventLocationResponse(BaseModel):
    id: str = ""
    name: Optional[str] = None


class EventResponse(BaseModel):
    id: UUID
    name: str
    type: EventType
    status: EventStatus
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    max_participants: Optional[int] = None
    image_path: Optional[str] = None
    location: EventLocationResponse
    description: Optional[str] = None
    btn_name: Optional[str] = None
    sort_order: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class EventsListResponse(BaseModel):
    success: bool = True
    message: str = "Active events fetched successfully"
    data: List[EventResponse]
    count: int
    pagination: Optional[PaginationMeta] = None


class EventEnrollResponse(BaseModel):
    id: UUID
    user_id: UUID
    event_id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EventEnrollCreateResponse(BaseModel):
    success: bool = True
    message: str = "Enrolled in event successfully"
    data: EventEnrollResponse
