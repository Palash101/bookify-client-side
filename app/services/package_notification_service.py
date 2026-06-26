"""Package notification helpers — Pub/Sub events on top of :class:`EventPublishService`."""
from __future__ import annotations

from typing import Optional, Union
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.events.event_payloads import build_package_notification_data
from app.core.events.event_types import (
    CLIENT_PACKAGE_PURCHASE_FAILED,
    CLIENT_PACKAGE_PURCHASED,
)
from app.services.event_publish_service import EventPublishService, PublishedEvent


class PackageNotificationService:
    @staticmethod
    async def publish_purchased(
        db: Session,
        *,
        tenant_id: str,
        user_package_id: Union[UUID, str],
    ) -> Optional[PublishedEvent]:
        return await EventPublishService.publish(
            tenant_id=tenant_id,
            event_type=CLIENT_PACKAGE_PURCHASED,
            data=build_package_notification_data(user_package_id=str(user_package_id)),
            ordering_key=str(tenant_id),
        )

    @staticmethod
    async def publish_purchase_failed(
        db: Session,
        *,
        tenant_id: str,
        user_package_id: Union[UUID, str],
    ) -> Optional[PublishedEvent]:
        return await EventPublishService.publish(
            tenant_id=tenant_id,
            event_type=CLIENT_PACKAGE_PURCHASE_FAILED,
            data=build_package_notification_data(user_package_id=str(user_package_id)),
            ordering_key=str(tenant_id),
        )
