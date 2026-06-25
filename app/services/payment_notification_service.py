"""Payment notification helpers — Pub/Sub events on top of :class:`EventPublishService`."""
from __future__ import annotations

from typing import Optional, Union
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.events.event_payloads import build_payment_notification_data
from app.core.events.event_types import CLIENT_PAYMENT_FAILED
from app.services.event_publish_service import EventPublishService, PublishedEvent


class PaymentNotificationService:
    @staticmethod
    async def publish_failed(
        db: Session,
        *,
        tenant_id: str,
        order_id: Union[UUID, str],
    ) -> Optional[PublishedEvent]:
        return await EventPublishService.publish_with_email_template(
            db,
            tenant_id=tenant_id,
            event_type=CLIENT_PAYMENT_FAILED,
            data=build_payment_notification_data(order_id=str(order_id)),
        )
