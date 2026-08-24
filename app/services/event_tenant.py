"""Resolve tenant_id for Pub/Sub events (sale row vs DB routing key)."""
from __future__ import annotations

from typing import Optional, Union
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.sales import Sale
from app.models.user_package import UserPackage


def event_tenant_id_from_sale(sale: Optional[Sale], fallback: str) -> str:
    if sale is not None and sale.tenant_id:
        return str(sale.tenant_id)
    return str(fallback)


def event_tenant_id_from_user_package_id(
    db: Session,
    user_package_id: Union[UUID, str],
    fallback: str,
) -> str:
    up = (
        db.query(UserPackage)
        .filter(UserPackage.id == user_package_id)
        .first()
    )
    if up is not None and up.sale_id:
        sale = db.query(Sale).filter(Sale.id == up.sale_id).first()
        return event_tenant_id_from_sale(sale, fallback)
    return str(fallback)
