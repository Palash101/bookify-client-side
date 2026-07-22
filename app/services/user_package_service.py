"""
Create user_packages rows when a package sale completes (wallet or gateway).
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.package import Package
from app.models.sales import Sale
from app.models.user_package import UserPackage
from app.services.user_package_tracking_service import record_package_purchase_credit


def ensure_user_package_for_completed_package_sale(
    db: Session,
    sale: Sale,
    *,
    created_by: Optional[str] = None,
    created_by_id: Optional[UUID] = None,
) -> Optional[UserPackage]:
    """
    Idempotent: one UserPackage per sale_id when the sale is a succeeded package purchase.
    """
    if sale.package_id is None:
        return None
    if sale.type not in ("package_gateway", "package_wallet") and not (
        sale.type == "gateway" and sale.product_item_type == "package"
    ) and not (
        sale.type == "wallet" and sale.product_item_type == "package"
    ):
        return None
    status_norm = (sale.status or "").lower()
    if status_norm not in ("succeeded", "success"):
        return None

    existing = db.query(UserPackage).filter(UserPackage.sale_id == sale.id).first()
    if existing:
        return existing

    session_total = sale.session_count
    row = UserPackage(
        user_id=sale.user_id,
        package_id=sale.package_id,
        pricing_id=sale.pricing_id,
        sale_id=sale.id,
        expire_at=sale.expires_at,
        session_count=session_total,
        total_session=session_total,
        session_type=sale.session_type,
        person_count=sale.person_count,
        created_by=created_by,
        created_by_id=created_by_id,
    )
    db.add(row)
    db.flush()

    package_name: Optional[str] = None
    if sale.package_id is not None:
        package = db.query(Package).filter(Package.id == sale.package_id).first()
        if package and package.name:
            package_name = package.name

    record_package_purchase_credit(
        db,
        user_package=row,
        sale=sale,
        package_name=package_name,
    )
    return row
