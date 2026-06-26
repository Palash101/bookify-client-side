"""Wallet notification helpers — Pub/Sub events on top of :class:`EventPublishService`."""
from __future__ import annotations

from typing import Optional, Union
from uuid import UUID

from sqlalchemy.orm import Session, attributes

from app.core.events.event_payloads import build_wallet_notification_data
from app.core.events.event_types import (
    CLIENT_WALLET_DEBITED,
    CLIENT_WALLET_TOPUP_FAILED,
    CLIENT_WALLET_TOPUP_SUCCESS,
)
from app.models.sales import Sale
from app.services.event_publish_service import EventPublishService, PublishedEvent

WALLET_TOPUP_EMAIL_SENT_KEY = "wallet_topup_email_sent"


def is_wallet_topup_sale(sale: Sale) -> bool:
    if (sale.product_item_type or "") == "wallet":
        return True
    meta = sale.extra_metadata or {}
    return (sale.type or "") == "gateway" and meta.get("purpose") == "wallet_add"


def wallet_topup_email_pending(sale: Sale) -> bool:
    if not sale.wallet_transaction_id:
        return False
    if not is_wallet_topup_sale(sale):
        return False
    if (sale.status or "").lower() not in ("succeeded", "success"):
        return False
    return not bool((sale.extra_metadata or {}).get(WALLET_TOPUP_EMAIL_SENT_KEY))


def mark_wallet_topup_email_sent(sale: Sale) -> None:
    meta = dict(sale.extra_metadata or {})
    meta[WALLET_TOPUP_EMAIL_SENT_KEY] = True
    sale.extra_metadata = meta
    attributes.flag_modified(sale, "extra_metadata")


class WalletNotificationService:
    @staticmethod
    async def publish_topup_success(
        db: Session,
        *,
        tenant_id: str,
        wallet_transaction_id: Union[UUID, str],
    ) -> Optional[PublishedEvent]:
        return await EventPublishService.publish(
            tenant_id=tenant_id,
            event_type=CLIENT_WALLET_TOPUP_SUCCESS,
            data=build_wallet_notification_data(
                wallet_transaction_id=str(wallet_transaction_id),
            ),
            ordering_key=str(tenant_id),
        )

    @staticmethod
    async def publish_topup_failed(
        db: Session,
        *,
        tenant_id: str,
        wallet_transaction_id: Union[UUID, str],
    ) -> Optional[PublishedEvent]:
        return await EventPublishService.publish(
            tenant_id=tenant_id,
            event_type=CLIENT_WALLET_TOPUP_FAILED,
            data=build_wallet_notification_data(
                wallet_transaction_id=str(wallet_transaction_id),
            ),
            ordering_key=str(tenant_id),
        )

    @staticmethod
    async def publish_debited(
        db: Session,
        *,
        tenant_id: str,
        wallet_transaction_id: Union[UUID, str],
    ) -> Optional[PublishedEvent]:
        return await EventPublishService.publish(
            tenant_id=tenant_id,
            event_type=CLIENT_WALLET_DEBITED,
            data=build_wallet_notification_data(
                wallet_transaction_id=str(wallet_transaction_id),
            ),
            ordering_key=str(tenant_id),
        )
