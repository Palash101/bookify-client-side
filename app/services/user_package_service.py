"""
Create user_packages rows when a package sale completes (wallet or gateway).
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.package import Package
from app.models.package_pricing import PackagePricing
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
from app.services.user_package_tracking_service import (
    backfill_missing_booking_debits,
    record_package_purchase_credit,
)


def _session_total_from_pricing(db: Session, pricing_id: Optional[UUID]) -> Optional[int]:
    if pricing_id is None:
        return None
    pricing = db.query(PackagePricing).filter(PackagePricing.id == pricing_id).first()
    if pricing is None or pricing.is_unlimited:
        return None
    if pricing.session_count is None:
        return None
    try:
        return int(pricing.session_count)
    except (TypeError, ValueError):
        return None


def _resolve_package_session_total(
    db: Session,
    sale: Sale,
    snap: dict[str, Any],
    pricing_id: Optional[UUID],
) -> Optional[int]:
    raw = snap.get("session_count")
    if raw is None:
        raw = sale_session_count(db, sale)
    if raw is None:
        raw = _session_total_from_pricing(db, pricing_id)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _backfill_user_package_sessions(
    db: Session,
    user_package: UserPackage,
    session_total: Optional[int],
) -> None:
    if session_total is None:
        return
    if user_package.total_session is None:
        user_package.total_session = session_total
    if user_package.session_count is None:
        user_package.session_count = session_total
    db.flush()


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

    snap = dict(snapshot or sale_txn_snapshot(db, sale))
    pricing_id = parse_pricing_id(snap.get("package_pricing_id") or snap.get("pricing_id"))
    session_total = _resolve_package_session_total(db, sale, snap, pricing_id)

    session_type = snap.get("session_type") or sale_session_type(db, sale)
    person_count = snap.get("persons")
    if person_count is None:
        person_count = snap.get("person_count")
    if person_count is None:
        person_count = sale_person_count(db, sale)

    package = db.query(Package).filter(Package.id == sale.package_id).first()
    package_name: Optional[str] = package.name if package and package.name else None

    existing = db.query(UserPackage).filter(UserPackage.sale_id == sale.id).first()
    if existing:
        _backfill_user_package_sessions(db, existing, session_total)
        record_package_purchase_credit(
            db,
            user_package=existing,
            sale=sale,
            package_name=package_name,
        )
        backfill_missing_booking_debits(db, user_package=existing, sale=sale)
        return existing

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

    record_package_purchase_credit(
        db,
        user_package=row,
        sale=sale,
        package_name=package_name,
    )
    return row
