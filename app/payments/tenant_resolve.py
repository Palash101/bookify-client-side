"""Resolve tenant for Stripe redirects/webhooks (no X-Tenant-Key on request)."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.db.master_db import SessionLocal as MasterSessionLocal
from app.core.db.session import get_session_factory
from app.models.master_org import Organization
from app.models.sales import Sale
from app.models.sales_transactions import SalesTransactions
from app.models.tenant_setting import TenantSetting
from app.payments.base import GatewayType

logger = logging.getLogger(__name__)


def _active_organization_ids() -> list[str]:
    db = MasterSessionLocal()
    try:
        rows = (
            db.query(Organization.organization_id)
            .filter(Organization.status == "active")
            .all()
        )
        return [str(r[0]) for r in rows if r[0]]
    finally:
        db.close()


def resolve_tenant_for_stripe_webhook(raw_body: bytes, stripe_signature: str) -> Optional[str]:
    """
    Stripe webhooks do not include X-Tenant-Key. Try each tenant DB until webhook
    signature verification succeeds with that tenant's webhook_secret.
    """
    try:
        import stripe as stripe_lib  # type: ignore
    except Exception:
        return None

    for tenant_id in _active_organization_ids():
        try:
            factory = get_session_factory(tenant_id)
            db = factory()
        except (ProgrammingError, OperationalError, RuntimeError):
            continue
        try:
            try:
                rows = (
                    db.query(TenantSetting)
                    .filter(
                        TenantSetting.setting_key == "payment_gateway",
                    )
                    .all()
                )
            except (ProgrammingError, OperationalError):
                continue

            for row in rows:
                config = row.value or {}
                if (config.get("type") or "").lower() != GatewayType.STRIPE.value:
                    continue
                secret = config.get("webhook_secret")
                if not secret:
                    continue
                try:
                    stripe_lib.Webhook.construct_event(raw_body, stripe_signature, secret)
                    return str(row.tenant_id or tenant_id)
                except Exception:
                    continue
        finally:
            db.close()

    return None


def resolve_tenant_for_stripe_session(session_id: str) -> Optional[str]:
    """Find which tenant DB holds this Stripe Checkout session id (cs_...)."""
    for tenant_id in _active_organization_ids():
        try:
            factory = get_session_factory(tenant_id)
            db = factory()
        except (ProgrammingError, OperationalError, RuntimeError):
            continue
        try:
            sale = (
                db.query(Sale)
                .filter(Sale.gateway_transaction_id == session_id)
                .first()
            )
            if sale is not None:
                return tenant_id
            st = (
                db.query(SalesTransactions)
                .filter(SalesTransactions.gateway_txn_id == session_id)
                .first()
            )
            if st is not None:
                return tenant_id
        except (ProgrammingError, OperationalError):
            continue
        finally:
            db.close()

    return None
