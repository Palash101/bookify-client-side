"""
Ledger helpers for user_package_tracking — session credits/debits on package entitlements.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, attributes

from app.models.class_booking import ClassBooking, ClassBookingStatus
from app.models.package import Package
from app.models.sales import Sale
from app.models.user_package import UserPackage
from app.models.user_package_tracking import (
    SessionTxnSource,
    SessionTxnType,
    UserPackageTracking,
)

_ACTIVE_PACKAGE_BOOKING_STATUSES = (
    ClassBookingStatus.confirmed,
    ClassBookingStatus.waiting,
    ClassBookingStatus.pending,
    ClassBookingStatus.pending_payment,
)


def get_user_package_for_sale(db: Session, sale_id: UUID) -> Optional[UserPackage]:
    return db.query(UserPackage).filter(UserPackage.sale_id == sale_id).first()


def _remaining_from_sale_metadata(sale: Sale) -> Optional[int]:
    meta = sale.extra_metadata or {}
    if not isinstance(meta, dict):
        return None
    for key in ("sessions_remaining", "remaining_sessions", "remaining_session", "sessions_left"):
        if key not in meta:
            continue
        raw = meta[key]
        if raw is None:
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return None


def _active_sessions_used(db: Session, sale_id: UUID) -> int:
    raw = (
        db.query(func.coalesce(func.sum(ClassBooking.sessions_deducted), 0))
        .filter(
            ClassBooking.user_package_purchase_id == sale_id,
            ClassBooking.status.in_(_ACTIVE_PACKAGE_BOOKING_STATUSES),
        )
        .scalar()
    )
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _ledger_debit_total(db: Session, user_package_id: UUID) -> int:
    raw = (
        db.query(func.coalesce(func.sum(UserPackageTracking.sessions), 0))
        .filter(
            UserPackageTracking.user_package_id == user_package_id,
            UserPackageTracking.txn_type == SessionTxnType.debit,
        )
        .scalar()
    )
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _package_session_total(user_package: UserPackage, sale: Sale) -> Optional[int]:
    if user_package.total_session is not None:
        return int(user_package.total_session)
    if sale.session_count is not None:
        return int(sale.session_count)
    return None


def sync_sale_sessions_remaining(sale: Sale, balance: Optional[int]) -> None:
    if balance is None:
        return
    meta: dict[str, Any] = dict(sale.extra_metadata or {})
    meta["sessions_remaining"] = max(0, int(balance))
    sale.extra_metadata = meta
    attributes.flag_modified(sale, "extra_metadata")


def sessions_remaining_for_sale(db: Session, sale: Sale) -> Optional[int]:
    """
    Remaining bookable sessions for a package sale.
    None means unlimited / unknown session pool.

    Uses total_session minus active booking usage so legacy rows (sessions_deducted
    without ledger entries) stay accurate.
    """
    user_package = get_user_package_for_sale(db, sale.id)
    if user_package is None:
        return _remaining_from_sale_metadata(sale)

    total = _package_session_total(user_package, sale)
    if total is not None:
        remaining = max(0, total - _active_sessions_used(db, sale.id))
        if user_package.session_count != remaining:
            user_package.session_count = remaining
            sync_sale_sessions_remaining(sale, remaining)
        return remaining

    if user_package.session_count is None:
        return None

    # Legacy rows without total_session: subtract bookings not yet in the ledger.
    used = _active_sessions_used(db, sale.id)
    ledger_debits = _ledger_debit_total(db, user_package.id)
    legacy_gap = max(0, used - ledger_debits)
    remaining = max(0, int(user_package.session_count) - legacy_gap)
    if user_package.session_count != remaining:
        user_package.session_count = remaining
        sync_sale_sessions_remaining(sale, remaining)
    return remaining


def _tracking_exists(
    db: Session,
    *,
    reference_id: UUID,
    txn_type: SessionTxnType,
    txn_source: SessionTxnSource,
) -> bool:
    return (
        db.query(UserPackageTracking.id)
        .filter(
            UserPackageTracking.reference_id == reference_id,
            UserPackageTracking.txn_type == txn_type,
            UserPackageTracking.txn_source == txn_source,
        )
        .first()
        is not None
    )


def _resolved_session_total(sale: Sale, user_package: UserPackage) -> Optional[int]:
    total = _package_session_total(user_package, sale)
    if total is not None:
        return total
    if user_package.session_count is not None:
        return int(user_package.session_count)
    return None


def record_package_purchase_credit(
    db: Session,
    *,
    user_package: UserPackage,
    sale: Sale,
    package_name: Optional[str] = None,
) -> Optional[UserPackageTracking]:
    """
    Package buy (wallet/gateway): credit / purchase / package sessions.
    Idempotent per sale id.
    """
    if _tracking_exists(
        db,
        reference_id=sale.id,
        txn_type=SessionTxnType.credit,
        txn_source=SessionTxnSource.purchase,
    ):
        return None

    sessions = _resolved_session_total(sale, user_package)
    if sessions is None:
        return None

    user_package.session_count = sessions
    if getattr(user_package, "total_session", None) is None:
        user_package.total_session = sessions

    sync_sale_sessions_remaining(sale, sessions)

    label = (package_name or "Package").strip() or "Package"
    row = UserPackageTracking(
        user_id=user_package.user_id,
        user_package_id=user_package.id,
        txn_type=SessionTxnType.credit,
        txn_source=SessionTxnSource.purchase,
        sessions=sessions,
        balance_after=sessions,
        reference_id=sale.id,
        notes=f"Package purchased: {label}",
    )
    db.add(row)
    db.flush()
    return row


def record_booking_debit(
    db: Session,
    *,
    user_package: UserPackage,
    sale: Sale,
    booking: ClassBooking,
    sessions: int = 1,
    notes: str = "Class booking",
) -> Optional[UserPackageTracking]:
    """
    Confirmed booking or waiting→confirmed: debit / booking / 1.
    Idempotent per booking id.
    """
    if sessions <= 0:
        return None
    if _tracking_exists(
        db,
        reference_id=booking.id,
        txn_type=SessionTxnType.debit,
        txn_source=SessionTxnSource.booking,
    ):
        return None

    remaining = sessions_remaining_for_sale(db, sale)
    if remaining is None:
        return None
    if remaining < sessions:
        return None

    balance_after = max(0, int(remaining) - sessions)
    user_package.session_count = balance_after
    sync_sale_sessions_remaining(sale, balance_after)

    row = UserPackageTracking(
        user_id=booking.user_id,
        user_package_id=user_package.id,
        txn_type=SessionTxnType.debit,
        txn_source=SessionTxnSource.booking,
        sessions=sessions,
        balance_after=balance_after,
        reference_id=booking.id,
        notes=notes,
    )
    db.add(row)
    db.flush()
    return row


def record_booking_refund_credit(
    db: Session,
    *,
    user_package: UserPackage,
    sale: Sale,
    booking: ClassBooking,
    sessions: int = 1,
) -> Optional[UserPackageTracking]:
    """
    Cancel within free window: credit / refund / 1.
    Idempotent per booking id.
    """
    if sessions <= 0:
        return None
    if _tracking_exists(
        db,
        reference_id=booking.id,
        txn_type=SessionTxnType.credit,
        txn_source=SessionTxnSource.refund,
    ):
        return None

    current = user_package.session_count
    if current is None:
        return None

    balance_after = int(current) + sessions
    user_package.session_count = balance_after
    sync_sale_sessions_remaining(sale, balance_after)

    row = UserPackageTracking(
        user_id=booking.user_id,
        user_package_id=user_package.id,
        txn_type=SessionTxnType.credit,
        txn_source=SessionTxnSource.refund,
        sessions=sessions,
        balance_after=balance_after,
        reference_id=booking.id,
        notes="Booking cancelled — session refunded",
    )
    db.add(row)
    db.flush()
    return row


def record_late_cancel_audit(
    db: Session,
    *,
    user_package: UserPackage,
    booking: ClassBooking,
) -> Optional[UserPackageTracking]:
    """
    Late cancel (audit only): credit / booking / 0 — no session restored.
    Idempotent per booking id.
    """
    if _tracking_exists(
        db,
        reference_id=booking.id,
        txn_type=SessionTxnType.credit,
        txn_source=SessionTxnSource.booking,
    ):
        return None

    balance_after = user_package.session_count
    row = UserPackageTracking(
        user_id=booking.user_id,
        user_package_id=user_package.id,
        txn_type=SessionTxnType.credit,
        txn_source=SessionTxnSource.booking,
        sessions=0,
        balance_after=balance_after,
        reference_id=booking.id,
        notes=(
            "Late cancellation: booking cancelled; package session was not refunded "
            "(outside free-cancel window)."
        ),
    )
    db.add(row)
    db.flush()
    return row


def apply_package_session_debit_for_booking(
    db: Session,
    *,
    sale: Sale,
    booking: ClassBooking,
    notes: str = "Class booking",
) -> int:
    """
    Deduct one session for a package booking and write the ledger row.
    Returns sessions deducted (0 or 1).
    """
    user_package = get_user_package_for_sale(db, sale.id)
    if user_package is None:
        return 0

    if _tracking_exists(
        db,
        reference_id=booking.id,
        txn_type=SessionTxnType.debit,
        txn_source=SessionTxnSource.booking,
    ):
        return 1

    remaining = sessions_remaining_for_sale(db, sale)
    if remaining is not None and remaining < 1:
        return 0

    row = record_booking_debit(
        db,
        user_package=user_package,
        sale=sale,
        booking=booking,
        sessions=1,
        notes=notes,
    )
    return 1 if row is not None else 0
