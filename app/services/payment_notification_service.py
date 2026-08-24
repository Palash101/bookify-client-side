"""Payment notification helpers — Pub/Sub events on top of :class:`EventPublishService`."""
from __future__ import annotations

from typing import Optional, Union

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
        sales_transaction_id: Union[int, str],
    ) -> Optional[PublishedEvent]:
        return await EventPublishService.publish(
            tenant_id=tenant_id,
            event_type=CLIENT_PAYMENT_FAILED,
            data=build_payment_notification_data(
                sales_transaction_id=str(sales_transaction_id),
            ),
            ordering_key=str(tenant_id),
        )
