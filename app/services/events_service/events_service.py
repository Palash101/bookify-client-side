from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.event_challenge import (
    EventBadge,
    EventCondition,
    EventReward,
    EventRewardGrant,
    EventRewardRel,
    UserBadge,
)
from app.models.event_enroll import EventEnroll
from app.models.event_event import EventEvent, EventStatus, EventType
from app.models.location import Location
from app.schemas.event import EventResponse, MyBadgeResponse, MyEnrollmentResponse

ALREADY_ENROLLED = "You are already enrolled in this event"


class EventsService:
    @staticmethod
    def list_active_events(
        db: Session,
        tenant_id: str,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
        page: int = 1,
        limit: int = 20,
    ) -> tuple[List[EventEvent], int]:
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

        total = query.count()
        offset = (page - 1) * limit
        return query.offset(offset).limit(limit).all(), total

    @staticmethod
    def _parse_location_id(raw: Optional[str]) -> Optional[UUID]:
        if not raw:
            return None
        try:
            return UUID(str(raw))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _value(raw: Any) -> Any:
        return raw.value if hasattr(raw, "value") else raw

    @staticmethod
    def _conditions_list(raw: Any) -> List[Dict[str, Any]]:
        if isinstance(raw, list):
            return [c for c in raw if isinstance(c, dict)]
        if isinstance(raw, dict):
            return [raw]
        return []

    @staticmethod
    def target_from_conditions(raw: Any) -> int:
        """target = sum of minimum_quantity over the challenge conditions."""
        total = 0
        for cond in EventsService._conditions_list(raw):
            try:
                total += int(cond.get("minimum_quantity") or 0)
            except (TypeError, ValueError):
                continue
        return total

    @staticmethod
    def _load_conditions(db: Session, event_ids: List[UUID]) -> Dict[UUID, EventCondition]:
        """One condition row per challenge (newest wins if several slipped in)."""
        if not event_ids:
            return {}
        rows = (
            db.query(EventCondition)
            .filter(EventCondition.event_id.in_(event_ids))
            .order_by(EventCondition.created_at.asc())
            .all()
        )
        return {row.event_id: row for row in rows}

    @staticmethod
    def _load_rewards(db: Session, event_ids: List[UUID]) -> Dict[UUID, List[Dict[str, Any]]]:
        if not event_ids:
            return {}
        rows = (
            db.query(EventRewardRel.event_id, EventRewardRel.tier, EventReward.name, EventReward.type)
            .join(EventReward, EventReward.id == EventRewardRel.reward_id)
            .filter(EventRewardRel.event_id.in_(event_ids))
            .order_by(EventRewardRel.tier.asc(), EventReward.name.asc())
            .all()
        )
        out: Dict[UUID, List[Dict[str, Any]]] = {}
        for event_id, tier, name, rtype in rows:
            out.setdefault(event_id, []).append(
                {"name": name, "type": EventsService._value(rtype), "tier": tier}
            )
        return out

    @staticmethod
    def _enrolled_event_ids(db: Session, user_id: UUID, event_ids: List[UUID]) -> Set[UUID]:
        if not event_ids:
            return set()
        rows = (
            db.query(EventEnroll.event_id)
            .filter(EventEnroll.user_id == user_id, EventEnroll.event_id.in_(event_ids))
            .all()
        )
        return {row[0] for row in rows}

    @staticmethod
    def events_to_responses(
        db: Session,
        events: List[EventEvent],
        user_id: Optional[UUID] = None,
        enrolled_ids: Optional[Set[UUID]] = None,
    ) -> List[EventResponse]:
        """
        Build list items with a fixed number of queries (locations, conditions,
        rewards, my enrollments), independent of the number of events.
        `is_enrolled` is only set when `user_id` (or `enrolled_ids`) is given.
        """
        event_ids = [event.id for event in events]
        challenge_ids = [
            event.id
            for event in events
            if EventsService._value(event.type) == EventType.challenge.value
        ]

        loc_ids: List[UUID] = []
        for event in events:
            loc_id = EventsService._parse_location_id(event.location)
            if loc_id is not None:
                loc_ids.append(loc_id)

        locations: Dict[str, Location] = {}
        if loc_ids:
            rows = db.query(Location).filter(Location.id.in_(loc_ids)).all()
            locations = {str(row.id): row for row in rows}

        conditions = EventsService._load_conditions(db, challenge_ids)
        rewards = EventsService._load_rewards(db, challenge_ids)
        if enrolled_ids is None and user_id is not None:
            enrolled_ids = EventsService._enrolled_event_ids(db, user_id, event_ids)

        responses: List[EventResponse] = []
        for event in events:
            loc = locations.get(str(event.location)) if event.location else None
            is_challenge = EventsService._value(event.type) == EventType.challenge.value
            cond = conditions.get(event.id) if is_challenge else None
            payload = {
                "id": event.id,
                "name": event.name,
                "type": event.type,
                "status": event.status,
                "starts_at": event.starts_at,
                "ends_at": event.ends_at,
                "max_participants": event.max_participants,
                "image_path": event.image_path,
                "location": {
                    "id": str(loc.id) if loc else (event.location or ""),
                    "name": loc.name if loc else None,
                },
                "description": event.description,
                "btn_name": event.btn_name,
                "sort_order": event.sort_order,
                "created_at": event.created_at,
                "updated_at": event.updated_at,
                "finalized_at": event.finalized_at,
                "challenge": (
                    {
                        "challenge_type": EventsService._value(cond.challenge_type),
                        "mode": EventsService._value(cond.challenge_mode),
                        "conditions": EventsService._conditions_list(cond.conditions),
                        "target": EventsService.target_from_conditions(cond.conditions),
                    }
                    if cond is not None
                    else None
                ),
                "rewards": rewards.get(event.id, []) if is_challenge else [],
                "is_enrolled": (event.id in enrolled_ids) if enrolled_ids is not None else None,
            }
            responses.append(EventResponse.model_validate(payload))
        return responses

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _has_ended(event: EventEvent, now: datetime) -> bool:
        if event.finalized_at is not None:
            return True
        if EventsService._value(event.status) == EventStatus.completed.value:
            return True
        return event.ends_at is not None and now >= event.ends_at

    @staticmethod
    def _is_enrolled(db: Session, user_id: UUID, event_id: UUID) -> bool:
        return (
            db.query(EventEnroll.id)
            .filter(EventEnroll.user_id == user_id, EventEnroll.event_id == event_id)
            .first()
            is not None
        )

    @staticmethod
    def enroll_user(
        db: Session,
        tenant_id: str,
        user_id: UUID,
        event_id: UUID,
    ) -> EventEnroll:
        # Lock the event row (FOR NO KEY UPDATE): concurrent joins of the same
        # event serialize here, so the capacity count below can't be raced past
        # max_participants. NO KEY UPDATE doesn't block FK checks of other inserts.
        event = (
            db.query(EventEvent)
            .filter(
                EventEvent.id == event_id,
                EventEvent.tenant_id == tenant_id,
            )
            .with_for_update(key_share=True)
            .first()
        )
        try:
            if event is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Event not found",
                )
            if EventsService._value(event.type) != EventType.challenge.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Only challenges can be joined",
                )
            if EventsService._has_ended(event, EventsService._now()):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="This challenge has already ended",
                )
            if EventsService._value(event.status) != EventStatus.active.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Event is not open for enrollment",
                )

            if EventsService._is_enrolled(db, user_id, event_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ALREADY_ENROLLED,
                )

            # NULL or 0 = unlimited.
            if event.max_participants:
                enrolled_count = (
                    db.query(func.count(EventEnroll.id))
                    .filter(EventEnroll.event_id == event_id)
                    .scalar()
                )
                if int(enrolled_count or 0) >= int(event.max_participants):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Event has reached maximum participants",
                    )
        except HTTPException:
            db.rollback()  # release the row lock
            raise

        enrollment = EventEnroll(
            tenant_id=tenant_id,
            user_id=user_id,
            event_id=event_id,
        )
        db.add(enrollment)
        try:
            db.commit()
        except IntegrityError:
            # uq_event_enroll_event_user: a concurrent request enrolled first.
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ALREADY_ENROLLED,
            )
        db.refresh(enrollment)
        return enrollment

    @staticmethod
    def leave_event(db: Session, tenant_id: str, user_id: UUID, event_id: UUID) -> None:
        """Leave a challenge before it ends; finalized challenges are frozen."""
        enrollment = (
            db.query(EventEnroll)
            .filter(
                EventEnroll.tenant_id == tenant_id,
                EventEnroll.user_id == user_id,
                EventEnroll.event_id == event_id,
            )
            .first()
        )
        if enrollment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="You are not enrolled in this event",
            )
        event = db.query(EventEvent).filter(EventEvent.id == event_id).first()
        if event is not None and EventsService._has_ended(event, EventsService._now()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This challenge has already ended",
            )
        db.delete(enrollment)
        db.commit()

    @staticmethod
    def list_my_enrollments(
        db: Session,
        tenant_id: str,
        user_id: UUID,
        page: int = 1,
        limit: int = 20,
    ) -> tuple[List[MyEnrollmentResponse], int]:
        base = (
            db.query(EventEnroll, EventEvent)
            .join(EventEvent, EventEvent.id == EventEnroll.event_id)
            .filter(
                EventEnroll.tenant_id == tenant_id,
                EventEnroll.user_id == user_id,
                EventEvent.tenant_id == tenant_id,
            )
        )
        total = base.count()
        rows = (
            base.order_by(EventEnroll.created_at.desc(), EventEnroll.id.desc())
            .offset((page - 1) * limit)
            .limit(limit)
            .all()
        )
        if not rows:
            return [], total

        events = [event for _, event in rows]
        event_ids = [event.id for event in events]
        event_items = {
            item.id: item
            for item in EventsService.events_to_responses(
                db, events, enrolled_ids=set(event_ids)
            )
        }

        grants = (
            db.query(
                EventRewardGrant.event_id,
                EventRewardGrant.tier,
                EventRewardGrant.status,
                EventRewardGrant.reward_type,
                EventReward.name,
            )
            .join(EventReward, EventReward.id == EventRewardGrant.reward_id)
            .filter(
                EventRewardGrant.user_id == user_id,
                EventRewardGrant.event_id.in_(event_ids),
            )
            .order_by(EventRewardGrant.tier.asc(), EventReward.name.asc())
            .all()
        )
        won: Dict[UUID, List[Dict[str, Any]]] = {}
        for g_event_id, tier, g_status, rtype, name in grants:
            won.setdefault(g_event_id, []).append(
                {"name": name, "type": rtype, "tier": tier, "status": g_status}
            )

        items: List[MyEnrollmentResponse] = []
        for enrollment, event in rows:
            item = event_items[event.id]
            target = item.challenge.target if item.challenge else 0
            items.append(
                MyEnrollmentResponse.model_validate(
                    {
                        "event": item,
                        "enrolled_at": enrollment.created_at,
                        "progress_count": enrollment.progress_count or 0,
                        "target": target,
                        "eligible": enrollment.eligible_at is not None,
                        "eligible_at": enrollment.eligible_at,
                        "last_progress_at": enrollment.last_progress_at,
                        "final_rank": enrollment.final_rank,
                        "rewards_won": won.get(event.id, []),
                    }
                )
            )
        return items, total

    @staticmethod
    def list_my_badges(
        db: Session,
        tenant_id: str,
        user_id: UUID,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[List[MyBadgeResponse], int]:
        base = (
            db.query(UserBadge, EventBadge, EventEvent.name)
            .join(EventBadge, EventBadge.id == UserBadge.badge_id)
            .outerjoin(EventEvent, EventEvent.id == UserBadge.event_id)
            .filter(UserBadge.tenant_id == tenant_id, UserBadge.user_id == user_id)
        )
        total = base.count()
        rows = (
            base.order_by(UserBadge.awarded_at.desc(), UserBadge.id.desc())
            .offset((page - 1) * limit)
            .limit(limit)
            .all()
        )
        items = [
            MyBadgeResponse.model_validate(
                {
                    "id": ub.id,
                    "badge_id": badge.id,
                    "name": badge.name,
                    # No storage signer in the client backend: icon_path is
                    # returned as stored (same as event image_path).
                    "icon_path": badge.icon_path,
                    "icon_url": badge.icon_path,
                    "event_id": ub.event_id,
                    "event_name": event_name,
                    "awarded_at": ub.awarded_at,
                }
            )
            for ub, badge, event_name in rows
        ]
        return items, total
