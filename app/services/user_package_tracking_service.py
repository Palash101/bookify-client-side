"""
Ledger helpers for user_package_tracking — session credits/debits on package entitlements.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.class_booking import ClassBooking, ClassBookingStatus
from app.models.package import Package
from app.models.package_pricing import PackagePricing
from app.models.sales import Sale, sale_session_count
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
    ClassBookingStatus.completed,
)


def get_user_package_for_sale(db: Session, sale_id: UUID) -> Optional[UserPackage]:
    return db.query(UserPackage).filter(UserPackage.sale_id == sale_id).first()


def _remaining_from_sale_metadata(sale: Sale) -> Optional[int]:
    return None


def _booking_package_ref_ids(user_package: UserPackage) -> list[UUID]:
    """
    class_bookings.user_package_id is a FK to user_packages.id.
    Some code paths historically wrote sales.id there — match both.
    """
    ids: list[UUID] = [user_package.id]
    if user_package.sale_id is not None:
        ids.append(user_package.sale_id)
    return ids


def _active_sessions_used(db: Session, sale_id: UUID, user_package: Optional[UserPackage] = None) -> int:
    refs = _booking_package_ref_ids(user_package) if user_package is not None else [sale_id]
    raw = (
        db.query(func.coalesce(func.sum(ClassBooking.sessions_deducted), 0))
        .filter(
            ClassBooking.user_package_purchase_id.in_(refs),
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
    from sqlalchemy.orm import object_session as _object_session

    db = _object_session(sale)
    if db is not None:
        count = sale_session_count(db, sale)
        if count is not None:
            return int(count)
    return None


def sync_sale_sessions_remaining(sale: Sale, balance: Optional[int]) -> None:
    return None


def sessions_remaining_for_sale(db: Session, sale: Sale) -> Optional[int]:
    """
    Remaining bookable sessions for a package sale.
    None means unlimited / unknown session pool.

    ``user_packages.session_count`` is the live remaining balance (admin and
    booking flows decrement it). Do not recompute from class_bookings here:
    those rows store user_packages.id, not sales.id, so a sale-id lookup
    under-counts used sessions and can restore a spent balance.
    """
    user_package = get_user_package_for_sale(db, sale.id)
    if user_package is None:
        return _remaining_from_sale_metadata(sale)

    if user_package.session_count is not None:
        return max(0, int(user_package.session_count))

    total = _package_session_total(user_package, sale)
    if total is None:
        return None
    used = _active_sessions_used(db, sale.id, user_package)
    return max(0, total - used)


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


def _session_total_from_pricing(db: Session, user_package: UserPackage) -> Optional[int]:
    if user_package.pricing_id is None:
        return None
    pricing = db.query(PackagePricing).filter(PackagePricing.id == user_package.pricing_id).first()
    if pricing is None or pricing.is_unlimited:
        return None
    if pricing.session_count is None:
        return None
    try:
        return int(pricing.session_count)
    except (TypeError, ValueError):
        return None


def _resolved_session_total(db: Session, sale: Sale, user_package: UserPackage) -> Optional[int]:
    total = _package_session_total(user_package, sale)
    if total is not None:
        return total
    if user_package.session_count is not None:
        return int(user_package.session_count)
    return _session_total_from_pricing(db, user_package)


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

    sessions = _resolved_session_total(db, sale, user_package)
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
    notes: str = "Booking cancelled — session refunded",
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
        notes=notes,
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


def backfill_missing_booking_debits(
    db: Session,
    *,
    user_package: UserPackage,
    sale: Sale,
) -> None:
    """
    Write ledger debits for package bookings that deducted sessions but have no tracking row
    (e.g. when session_count was null at booking time).
    """
    refs = _booking_package_ref_ids(user_package)
    bookings = (
        db.query(ClassBooking)
        .filter(
            ClassBooking.user_package_id.in_(refs),
            ClassBooking.payment_mode == "package",
            ClassBooking.status.in_(_ACTIVE_PACKAGE_BOOKING_STATUSES),
            ClassBooking.sessions_deducted > 0,
        )
        .all()
    )
    for booking in bookings:
        if _tracking_exists(
            db,
            reference_id=booking.id,
            txn_type=SessionTxnType.debit,
            txn_source=SessionTxnSource.booking,
        ):
            continue
        record_booking_debit(
            db,
            user_package=user_package,
            sale=sale,
            booking=booking,
            sessions=int(booking.sessions_deducted or 1),
            notes="Class booking",
        )
