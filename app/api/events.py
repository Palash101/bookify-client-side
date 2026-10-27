from typing import Optional
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.dependencies import (
    get_current_active_user,
    get_current_tenant_id,
    get_optional_current_user,
)
from app.models.user import User
from app.schemas.event import (
    EventEnrollCreateResponse,
    EventEnrollResponse,
    EventLeaveResponse,
    EventsListResponse,
    MyEnrollmentsListResponse,
)
from app.schemas.transactions import build_pagination
from app.services.events_service.events_service import EventsService

router = APIRouter()


@router.get("", response_model=EventsListResponse)
def get_active_events(
    search: Optional[str] = Query(None, description="Search events by name"),
    sort_by: Optional[str] = Query(
        None, description="Sort by: name, starts_at, created_at, sort_order"
    ),
    sort_order: str = Query("asc", description="Sort direction: asc or desc"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    List active events for the current gym (X-Tenant-Key / request domain).
    Login is not required. Enroll still needs a Bearer token.

    Challenge events carry `challenge` and `rewards`. With a valid Bearer token
    each item also has `is_enrolled` (null for anonymous requests).
    """
    events, total = EventsService.list_active_events(
        db,
        tenant_id=tenant_id,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        limit=limit,
    )
    return {
        "success": True,
        "message": "Active events fetched successfully" if events else "No active events found",
        "data": EventsService.events_to_responses(
            db, events, user_id=current_user.id if current_user else None
        ),
        "count": len(events),
        "pagination": build_pagination(page, limit, total),
    }


@router.get("/me", response_model=MyEnrollmentsListResponse)
def get_my_enrollments(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    My event enrollments, newest first, with my own challenge progress and the
    rewards I won. Members never see other participants.
    """
    items, total = EventsService.list_my_enrollments(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        page=page,
        limit=limit,
    )
    return {
        "success": True,
        "message": "Enrollments fetched successfully" if items else "No enrollments found",
        "data": items,
        "count": len(items),
        "pagination": build_pagination(page, limit, total),
    }


@router.post("/{event_id}/enroll", response_model=EventEnrollCreateResponse)
def enroll_in_event(
    event_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Enroll the logged-in user in an active event for their gym.
    Requires Bearer token. X-Tenant-Key is only needed for app/middleware routing.
    """
    enrollment = EventsService.enroll_user(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        event_id=event_id,
    )
    return {
        "success": True,
        "message": "Enrolled in event successfully",
        "data": EventEnrollResponse.model_validate(enrollment),
    }


@router.delete("/{event_id}/enroll", response_model=EventLeaveResponse)
def leave_event(
    event_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Leave a challenge before it ends (not possible once it ended / was finalized).
    404 when not enrolled.
    """
    EventsService.leave_event(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        event_id=event_id,
    )
    return {"success": True, "message": "You left the challenge"}
