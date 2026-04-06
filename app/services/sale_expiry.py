"""
Expiry for package-purchase sales: derive from Package.validity_days / validity_end.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
import uuid

from sqlalchemy.orm import Session

from app.models.package import Package
from app.models.sales import Sale


def compute_sale_expires_at(order: Sale, package: Optional[Package]) -> Optional[datetime]:
    """
    Same rules as payment callback: created_at + validity_days, else end of validity_end day.
    """
    if package is None:
        return None
    if package.validity_days is not None and order.created_at is not None:
        return order.created_at + timedelta(days=package.validity_days)
    if package.validity_end is not None:
        tz = order.created_at.tzinfo if order.created_at else timezone.utc
        return datetime.combine(
            package.validity_end,
            datetime.max.time(),
            tzinfo=tz,
        )
    return None


def apply_package_expiry_to_sale(
    db: Session,
    order: Sale,
    tenant_id: uuid.UUID,
    *,
    overwrite: bool = False,
) -> None:
    """
    Set sale.expires_at from linked package when missing (or always if overwrite).
    """
    package = (
        db.query(Package)
        .filter(Package.id == order.package_id, Package.tenant_id == tenant_id)
        .first()
    )
    computed = compute_sale_expires_at(order, package)
    if computed is None:
        return
    if overwrite or order.expires_at is None:
        order.expires_at = computed
