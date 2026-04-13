"""
Create user_packages rows when a package sale completes (wallet or gateway).
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.sales import Sale
from app.models.user_package import UserPackage


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
    if sale.type not in ("package_gateway", "package_wallet"):
        return None
    status_norm = (sale.status or "").lower()
    if status_norm not in ("succeeded", "success"):
        return None

    existing = db.query(UserPackage).filter(UserPackage.sale_id == sale.id).first()
    if existing:
        return existing

    row = UserPackage(
        user_id=sale.user_id,
        package_id=sale.package_id,
        pricing_id=sale.pricing_id,
        sale_id=sale.id,
        expire_at=sale.expires_at,
        session_count=sale.session_count,
        session_type=sale.session_type,
        person_count=sale.person_count,
        created_by=created_by,
        created_by_id=created_by_id,
    )
    db.add(row)
    return row
