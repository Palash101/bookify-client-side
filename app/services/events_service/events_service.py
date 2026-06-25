from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.event_enroll import EventEnroll
from app.models.event_event import EventEvent, EventStatus


class EventsService:
    @staticmethod
    def list_active_events(
        db: Session,
        tenant_id: str,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
    ) -> List[EventEvent]:
        query = db.query(EventEvent).filter(
            EventEvent.tenant_id == tenant_id,
            EventEvent.status == EventStatus.active,
        )

        if search:
            like = f"%{search}%"
            query = query.filter(EventEvent.name.ilike(like))

        sort_column = None
        if sort_by == "name":
            sort_column = EventEvent.name
        elif sort_by == "starts_at":
            sort_column = EventEvent.starts_at
        elif sort_by == "created_at":
            sort_column = EventEvent.created_at
        elif sort_by == "sort_order":
            sort_column = EventEvent.sort_order

        if sort_column is not None:
            query = query.order_by(
                sort_column.asc() if sort_order.lower() == "asc" else sort_column.desc()
            )
        else:
            query = query.order_by(
                EventEvent.sort_order.asc().nullslast(),
                EventEvent.starts_at.asc().nullslast(),
                EventEvent.created_at.desc(),
            )

        return query.all()

    @staticmethod
    def enroll_user(
        db: Session,
        tenant_id: str,
        user_id: UUID,
        event_id: UUID,
    ) -> EventEnroll:
        event = (
            db.query(EventEvent)
            .filter(
                EventEvent.id == event_id,
                EventEvent.tenant_id == tenant_id,
            )
            .first()
        )
        if event is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Event not found",
            )
        if event.status != EventStatus.active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Event is not open for enrollment",
            )

        existing = (
            db.query(EventEnroll)
            .filter(
                EventEnroll.tenant_id == tenant_id,
                EventEnroll.user_id == user_id,
                EventEnroll.event_id == event_id,
            )
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You are already enrolled in this event",
            )

        if event.max_participants is not None:
            enrolled_count = (
                db.query(func.count(EventEnroll.id))
                .filter(
                    EventEnroll.tenant_id == tenant_id,
                    EventEnroll.event_id == event_id,
                )
                .scalar()
            )
            if int(enrolled_count or 0) >= int(event.max_participants):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Event has reached maximum participants",
                )

        enrollment = EventEnroll(
            tenant_id=tenant_id,
            user_id=user_id,
            event_id=event_id,
        )
        db.add(enrollment)
        db.commit()
        db.refresh(enrollment)
        return enrollment
