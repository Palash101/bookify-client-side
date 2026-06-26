"""Wallet notification helpers — Pub/Sub events on top of :class:`EventPublishService`."""
from __future__ import annotations

from typing import Optional, Union
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.events.event_payloads import build_wallet_notification_data
from app.core.events.event_types import (
    CLIENT_WALLET_DEBITED,
    CLIENT_WALLET_TOPUP_FAILED,
    CLIENT_WALLET_TOPUP_SUCCESS,
)
from app.services.event_publish_service import EventPublishService, PublishedEvent


class WalletNotificationService:
    @staticmethod
    async def publish_topup_success(
        db: Session,
        *,
        tenant_id: str,
        wallet_transaction_id: Union[UUID, str],
    ) -> Optional[PublishedEvent]:
        return await EventPublishService.publish_with_email_template(
            db,
            tenant_id=tenant_id,
            event_type=CLIENT_WALLET_TOPUP_SUCCESS,
            data=build_wallet_notification_data(
                wallet_transaction_id=str(wallet_transaction_id),
            ),
        )

    @staticmethod
    async def publish_topup_failed(
        db: Session,
        *,
        tenant_id: str,
        wallet_transaction_id: Union[UUID, str],
    ) -> Optional[PublishedEvent]:
        return await EventPublishService.publish_with_email_template(
            db,
            tenant_id=tenant_id,
            event_type=CLIENT_WALLET_TOPUP_FAILED,
            data=build_wallet_notification_data(
                wallet_transaction_id=str(wallet_transaction_id),
            ),
        )

    @staticmethod
    async def publish_debited(
        db: Session,
        *,
        tenant_id: str,
        wallet_transaction_id: Union[UUID, str],
    ) -> Optional[PublishedEvent]:
        return await EventPublishService.publish_with_email_template(
            db,
            tenant_id=tenant_id,
            event_type=CLIENT_WALLET_DEBITED,
            data=build_wallet_notification_data(
                wallet_transaction_id=str(wallet_transaction_id),
            ),
        )
