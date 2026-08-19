"""Wallet notification helpers — Pub/Sub events on top of :class:`EventPublishService`."""
from __future__ import annotations

from typing import Optional, Union
from uuid import UUID

from sqlalchemy.orm import Session, object_session

from app.core.events.event_payloads import build_wallet_notification_data
from app.core.events.event_types import (
    CLIENT_WALLET_DEBITED,
    CLIENT_WALLET_TOPUP_FAILED,
    CLIENT_WALLET_TOPUP_SUCCESS,
)
from app.models.sales import Sale, latest_sales_transaction, sale_status_value
from app.services.event_publish_service import EventPublishService, PublishedEvent

WALLET_TOPUP_EMAIL_SENT_KEY = "wallet_topup_email_sent"


def is_wallet_topup_sale(sale: Sale) -> bool:
    return (sale.product_item_type or "") == "wallet"


def wallet_topup_email_pending(sale: Sale) -> bool:
    if not sale.wallet_transaction_id:
        return False
    if not is_wallet_topup_sale(sale):
        return False
    db = object_session(sale)
    if db is None:
        return False
    if (sale_status_value(db, sale) or "").lower() not in ("succeeded", "success"):
        return False
    txn = latest_sales_transaction(db, sale.id)
    meta = txn.extra_metadata if txn is not None else None
    return not bool((meta or {}).get(WALLET_TOPUP_EMAIL_SENT_KEY))


def mark_wallet_topup_email_sent(sale: Sale) -> None:
    db = object_session(sale)
    if db is None:
        return
    txn = latest_sales_transaction(db, sale.id)
    if txn is None:
        return
    meta = dict(txn.extra_metadata or {})
    meta[WALLET_TOPUP_EMAIL_SENT_KEY] = True
    txn.extra_metadata = meta


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
