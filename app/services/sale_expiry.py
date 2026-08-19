"""
Expiry for package-purchase sales: derive from Package.validity_days / validity_end.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.package import Package
from app.models.sales import Sale
from app.models.user_package import UserPackage


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
    tenant_id: str,
    *,
    overwrite: bool = False,
) -> None:
    """
    Set user_packages.expire_at from linked package when missing (or always if overwrite).
    """
    package = (
        db.query(Package)
        .filter(Package.id == order.package_id, Package.tenant_id == tenant_id)
        .first()
    )
    computed = compute_sale_expires_at(order, package)
    if computed is None:
        return
    up = db.query(UserPackage).filter(UserPackage.sale_id == order.id).first()
    if up is None:
        return
    if overwrite or up.expire_at is None:
        up.expire_at = computed
