"""
Create user_packages rows when a package sale completes (wallet or gateway).
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.package import Package
from app.models.sales import (
    Sale,
    is_package_sale,
    parse_pricing_id,
    sale_person_count,
    sale_session_count,
    sale_session_type,
    sale_status_value,
    sale_txn_snapshot,
)
from app.models.user_package import UserPackage
from app.services.sale_expiry import compute_sale_expires_at
from app.services.user_package_tracking_service import record_package_purchase_credit


def ensure_user_package_for_completed_package_sale(
    db: Session,
    sale: Sale,
    *,
    created_by: Optional[str] = None,
    created_by_id: Optional[UUID] = None,
    snapshot: Optional[dict[str, Any]] = None,
) -> Optional[UserPackage]:
    """
    Idempotent: one UserPackage per sale_id when the sale is a succeeded package purchase.
    Session/expiry snapshot comes from sales_transactions.extra_metadata or ``snapshot``.
    """
    if sale.package_id is None:
        return None
    if not is_package_sale(sale):
        return None
    status_norm = (sale_status_value(db, sale) or "").lower()
    if snapshot is None and status_norm not in ("succeeded", "success"):
        return None

    existing = db.query(UserPackage).filter(UserPackage.sale_id == sale.id).first()
    if existing:
        return existing

    snap = dict(snapshot or sale_txn_snapshot(db, sale))
    session_total = snap.get("session_count")
    if session_total is None:
        session_total = sale_session_count(db, sale)
    if session_total is not None:
        try:
            session_total = int(session_total)
        except (TypeError, ValueError):
            session_total = None

    session_type = snap.get("session_type") or sale_session_type(db, sale)
    person_count = snap.get("persons")
    if person_count is None:
        person_count = snap.get("person_count")
    if person_count is None:
        person_count = sale_person_count(db, sale)

    pricing_id = parse_pricing_id(snap.get("package_pricing_id") or snap.get("pricing_id"))

    package = db.query(Package).filter(Package.id == sale.package_id).first()
    expire_at = compute_sale_expires_at(sale, package)

    row = UserPackage(
        user_id=sale.user_id,
        package_id=sale.package_id,
        pricing_id=pricing_id,
        sale_id=sale.id,
        expire_at=expire_at,
        session_count=session_total,
        total_session=session_total,
        session_type=session_type,
        person_count=person_count,
        created_by=created_by,
        created_by_id=created_by_id,
    )
    db.add(row)
    db.flush()

    package_name: Optional[str] = None
    if package and package.name:
        package_name = package.name

    record_package_purchase_credit(
        db,
        user_package=row,
        sale=sale,
        package_name=package_name,
    )
    return row
