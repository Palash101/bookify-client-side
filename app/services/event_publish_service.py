"""Reusable Pub/Sub event publishing for any feature (booking, OTP, wallet, etc.)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from sqlalchemy.orm import Session

from app.core.events import publish_event
from app.core.logging import get_logger
from app.models.notification_template import NotificationTemplate, NotificationTemplateType

log = get_logger(__name__)


@dataclass(frozen=True)
class PublishedEvent:
    """Result of a successful Pub/Sub publish."""

    event_id: str
    message_id: str
    event_type: str
    tenant_id: str


class EventPublishService:
    @staticmethod
    def new_event_id() -> str:
        """Fresh UUID for every outbound event."""
        return str(uuid.uuid4())

    @staticmethod
    def get_active_email_templates(
        db: Session,
        *,
        tenant_id: str,
        event_type: str,
    ) -> Sequence[NotificationTemplate]:
        return (
            db.query(NotificationTemplate)
            .filter(
                NotificationTemplate.tenant_id == tenant_id,
                NotificationTemplate.event_type == event_type,
                NotificationTemplate.template_type == NotificationTemplateType.email,
                NotificationTemplate.is_active.is_(True),
            )
            .all()
        )

    @staticmethod
    async def publish(
        *,
        tenant_id: str,
        event_type: str,
        data: dict[str, Any],
        ordering_key: Optional[str] = None,
    ) -> PublishedEvent:
        """
        Publish any tenant event. Always assigns a new unique ``event_id``.

        Use from OTP, wallet, package, or any flow that already has full ``data``.
        """
        event_id = EventPublishService.new_event_id()
        message_id = await publish_event(
            event_type=event_type,
            tenant_id=str(tenant_id),
            data=data,
            ordering_key=ordering_key or str(tenant_id),
            event_id=event_id,
        )
        log.info(
            "event_published tenant_id=%s event_type=%s event_id=%s message_id=%s",
            tenant_id,
            event_type,
            event_id,
            message_id,
        )
        return PublishedEvent(
            event_id=event_id,
            message_id=message_id,
            event_type=event_type,
            tenant_id=str(tenant_id),
        )

    @staticmethod
    async def publish_with_email_template(
        db: Session,
        *,
        tenant_id: str,
        event_type: str,
        data: dict[str, Any],
        ordering_key: Optional[str] = None,
    ) -> Optional[PublishedEvent]:
        """
        Publish tenant events to Pub/Sub (same as OTP).

        Logs a warning when no active email template exists in DB — consumer still
        receives the event and resolves templates on its side. Template lookup here
        is advisory only; we do not block publish (that caused silent drops vs OTP).
        """
        templates = EventPublishService.get_active_email_templates(
            db, tenant_id=tenant_id, event_type=event_type
        )
        if not templates:
            log.warning(
                "event_publish_no_active_template tenant_id=%s event_type=%s data=%s "
                "(publishing anyway; consumer may skip email)",
                tenant_id,
                event_type,
                data,
            )

        result = await EventPublishService.publish(
            tenant_id=tenant_id,
            event_type=event_type,
            data=data,
            ordering_key=ordering_key,
        )
        log.info(
            "event_published_with_template tenant_id=%s event_type=%s event_id=%s "
            "template_count=%s message_id=%s",
            tenant_id,
            event_type,
            result.event_id,
            len(templates),
            result.message_id,
        )
        return result
