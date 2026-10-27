from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel

from app.models.event_event import EventStatus, EventType
from app.schemas.transactions import PaginationMeta


class EventLocationResponse(BaseModel):
    id: str = ""
    name: Optional[str] = None


class EventChallengeResponse(BaseModel):
    challenge_type: str  # booking | package
    mode: str  # any_booking | training_program | time_based | any_package | selected_package
    conditions: List[Dict[str, Any]] = []
    target: int = 0  # sum of minimum_quantity


class EventRewardResponse(BaseModel):
    name: str
    type: str  # wallet | package | badge
    tier: int  # tier N goes to rank N


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
    finalized_at: Optional[datetime] = None
    challenge: Optional[EventChallengeResponse] = None  # challenge events only
    rewards: List[EventRewardResponse] = []
    is_enrolled: Optional[bool] = None  # null when the request is anonymous

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


class EventRewardWonResponse(BaseModel):
    name: str
    type: str
    tier: int
    status: str  # pending | granted | failed


class MyEnrollmentResponse(BaseModel):
    event: EventResponse
    enrolled_at: datetime
    progress_count: int = 0
    target: int = 0
    eligible: bool = False
    eligible_at: Optional[datetime] = None
    last_progress_at: Optional[datetime] = None
    final_rank: Optional[int] = None
    rewards_won: List[EventRewardWonResponse] = []


class MyEnrollmentsListResponse(BaseModel):
    success: bool = True
    message: str = "Enrollments fetched successfully"
    data: List[MyEnrollmentResponse]
    count: int
    pagination: Optional[PaginationMeta] = None


class EventLeaveResponse(BaseModel):
    success: bool = True
    message: str = "You left the challenge"


class MyBadgeResponse(BaseModel):
    id: UUID
    badge_id: UUID
    name: str
    icon_path: Optional[str] = None
    icon_url: Optional[str] = None
    event_id: Optional[UUID] = None
    event_name: Optional[str] = None
    awarded_at: datetime


class MyBadgesListResponse(BaseModel):
    success: bool = True
    message: str = "Badges fetched successfully"
    data: List[MyBadgeResponse]
    count: int
    pagination: Optional[PaginationMeta] = None
