from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as time_type, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Any, Optional, Sequence, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import String as SAString, and_, cast, func, or_
from sqlalchemy.exc import ProgrammingError, OperationalError
from sqlalchemy.orm import Session, aliased, attributes

from app.models.class_booking import (
    ClassBooking,
    ClassBookingStatus,
    class_booking_status_value,
    normalize_class_booking_status,
)
from app.models.user import normalize_user_gender
from app.models.fitness_program import FitnessProgram
from app.models.gym_class import GymClass
from app.models.sales import Sale, sale_expires_at, sale_succeeded_clause
from app.models.user import User
from app.models.wallet_transactions import WalletTransaction
from fastapi import HTTPException, status

from app.core.settings import settings
from app.schemas.booking import PaymentMode
from app.schemas.gym_config_value import GymConfigValue
from app.services.fitness_programs_service.fitness_programs_service import FitnessProgramsService
from app.services.gym_config_service import GymConfigService
from app.services.user_package_tracking_service import (
    apply_package_session_debit_for_booking,
    get_user_package_for_sale,
    record_booking_refund_credit,
    record_late_cancel_audit,
    sessions_remaining_for_sale,
)

logger = logging.getLogger(__name__)


def _append_bfy_wtxn_note(existing: Optional[str], txn_id: UUID, kind: str) -> str:
    """Machine-readable marker for wallet txns without a linked Sale row."""
    tag = f"__bfy_wtxn:{txn_id}:{kind}"
    base = (existing or "").strip()
    if not base:
        return tag
    if tag in base:
        return base
    return f"{base}\n{tag}"


_BFY_WTXN_DEBIT_RE = re.compile(
    r"__bfy_wtxn:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):debit",
    re.IGNORECASE,
)


def _wallet_debit_txn_id_from_notes(notes: Optional[str]) -> Optional[UUID]:
    if not notes:
        return None
    match = _BFY_WTXN_DEBIT_RE.search(notes)
    if not match:
        return None
    try:
        return UUID(match.group(1))
    except (TypeError, ValueError):
        return None


def _wallet_refund_already_recorded(notes: Optional[str]) -> bool:
    return bool(notes and "__bfy_wtxn:" in notes and ":refund" in notes)


def _wallet_user(db: Session, user: User) -> User:
    """Load user in the active DB session before wallet balance updates."""
    db_user = db.get(User, user.id)
    if db_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return db_user


def _location_id_for_class(
    db: Session, gym_class: GymClass, tenant_id: str
) -> Optional[UUID]:
    """gym_classes has no location_id; resolve via training_programme -> fitness_programs."""
    prog_id = getattr(gym_class, "training_programme_id", None)
    try:
        prog_id_int = int(prog_id) if prog_id is not None else 0
    except (TypeError, ValueError):
        prog_id_int = 0
    if prog_id_int <= 0:
        return None
    row = (
        db.query(FitnessProgram.location_id)
        .filter(
            FitnessProgram.id == prog_id_int,
            FitnessProgram.tenant_id == tenant_id,
        )
        .first()
    )
    return row.location_id if row else None


def _wallet_debit_consumed(
    db: Session,
    user_id: UUID,
    debit_id: UUID,
    *,
    exclude_booking_id: UUID,
) -> bool:
    """True when another booking row already references this wallet debit."""
    marker = f"__bfy_wtxn:{debit_id}:debit"
    return (
        db.query(ClassBooking.id)
        .filter(
            ClassBooking.user_id == user_id,
            ClassBooking.id != exclude_booking_id,
            ClassBooking.notes.contains(marker),
        )
        .first()
        is not None
    )


def _resolve_wallet_debit_txn(
    db: Session,
    *,
    user: User,
    booking: ClassBooking,
    gym_class: GymClass,
) -> Optional[WalletTransaction]:
    """Find the wallet debit for a class booking (notes marker, else time+amount fallback)."""
    debit_id = _wallet_debit_txn_id_from_notes(booking.notes)
    if debit_id is not None:
        txn = (
            db.query(WalletTransaction)
            .filter(
                WalletTransaction.id == debit_id,
                WalletTransaction.user_id == user.id,
                WalletTransaction.direction == "debit",
            )
            .first()
        )
        if txn:
            return txn

    price = Decimal(str(gym_class.price or 0))
    if price <= 0:
        return None
    ref_time = booking.booked_at or booking.created_at
    if ref_time is None:
        return None

    window = timedelta(minutes=15)
    candidates = (
        db.query(WalletTransaction)
        .filter(
            WalletTransaction.user_id == user.id,
            WalletTransaction.direction == "debit",
            WalletTransaction.amount == price,
            WalletTransaction.created_at >= ref_time - window,
            WalletTransaction.created_at <= ref_time + window,
        )
        .order_by(WalletTransaction.created_at.asc())
        .all()
    )
    for txn in candidates:
        if _wallet_debit_consumed(db, user.id, txn.id, exclude_booking_id=booking.id):
            continue
        if debit_id is None:
            booking.notes = _append_bfy_wtxn_note(booking.notes, txn.id, "debit")
        return txn
    return None


def _refund_wallet_for_cancelled_booking(
    db: Session,
    *,
    user: User,
    booking: ClassBooking,
    gym_class: GymClass,
    within_free_window: bool,
) -> Optional[UUID]:
    if (booking.payment_mode or "").strip().lower() != "wallet":
        return None
    if not within_free_window:
        return None
    if _wallet_refund_already_recorded(booking.notes):
        return None

    debit_txn = _resolve_wallet_debit_txn(
        db, user=user, booking=booking, gym_class=gym_class
    )
    if not debit_txn:
        return None

    amount = Decimal(str(debit_txn.amount or 0))
    if amount <= 0:
        return None

    db_user = _wallet_user(db, user)
    bal_before = Decimal(str(db_user.wallet or 0))
    bal_after = bal_before + amount
    refund_txn = WalletTransaction(
        user_id=db_user.id,
        direction="credit",
        transaction_id=None,
        amount=amount,
        currency=debit_txn.currency,
        balance_before=bal_before,
        balance_after=bal_after,
        created_by=db_user.user_type or "member",
        created_by_id=db_user.id,
    )
    db.add(refund_txn)
    db.flush()
    db_user.wallet = bal_after
    booking.notes = _append_bfy_wtxn_note(booking.notes, refund_txn.id, "refund")
    return refund_txn.id


# Bookings that block the user from booking the same class again
ACTIVE_USER_BOOKING_STATUSES: Tuple[ClassBookingStatus, ...] = (
    ClassBookingStatus.confirmed,
    ClassBookingStatus.waiting,
    ClassBookingStatus.pending,
    ClassBookingStatus.pending_payment,
)

# Statuses that hold a regular slot (not waitlist).
OCCUPYING_SLOT_STATUSES: Tuple[ClassBookingStatus, ...] = (
    ClassBookingStatus.confirmed,
    ClassBookingStatus.pending,
    ClassBookingStatus.pending_payment,
)

WAITING_STATUS = ClassBookingStatus.waiting
CANCELLED_STATUS = ClassBookingStatus.cancelled

# Wallet is charged upfront for any active booking that reserves the member's spot.
WALLET_CHARGE_STATUSES: Tuple[ClassBookingStatus, ...] = (
    ClassBookingStatus.confirmed,
    ClassBookingStatus.pending,
    WAITING_STATUS,
)
_WALLET_CHARGE_STATUS_VALUES = frozenset(s.value for s in WALLET_CHARGE_STATUSES)


def _tenant_tz(
    db: Session,
    tenant_id: str,
    gym_config: Optional[GymConfigValue] = None,
) -> ZoneInfo:
    cfg = gym_config if gym_config is not None else GymConfigService.get_gym_config(db, tenant_id)
    return GymConfigService.resolve_zoneinfo(cfg)


def _class_starts_at(gym_class: GymClass, tz: ZoneInfo) -> Optional[datetime]:
    if not gym_class.class_date or not gym_class.start_time:
        return None
    d: date = gym_class.class_date
    t: time_type = gym_class.start_time
    return datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=tz)


def booking_cancel_info(
    booking: ClassBooking,
    gym_class: Optional[GymClass],
    gym_config: GymConfigValue,
    tz: ZoneInfo,
    now: Optional[datetime] = None,
) -> tuple[bool, Optional[str]]:
    """Return (can_cancel, cancel_deadline_iso) for a booking on a scheduled class."""
    if now is None:
        now = datetime.now(tz)

    cancel_deadline_iso: Optional[str] = None
    can_cancel = False
    starts_at = _class_starts_at(gym_class, tz) if gym_class is not None else None

    if booking.status != CANCELLED_STATUS and starts_at is not None:
        cancel_hours = int(gym_config.booking_settings.cancellation_window_hours or 0)
        allow_late = bool(gym_config.booking_settings.allow_late_cancellations)
        cutoff = starts_at - timedelta(hours=cancel_hours) if cancel_hours > 0 else starts_at
        cancel_deadline_iso = cutoff.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")
        if allow_late:
            can_cancel = booking.status not in (ClassBookingStatus.completed,)
        else:
            can_cancel = now <= cutoff and booking.status not in (ClassBookingStatus.completed,)

    return can_cancel, cancel_deadline_iso


def _is_cancelled_class(status_value: Optional[str]) -> bool:
    s = (status_value or "").strip().lower()
    return s in ("cancelled", "canceled")


def _is_inactive_class(status_value: Optional[str]) -> bool:
    s = (status_value or "").strip().lower()
    return s in ("inactive", "disabled", "deleted")


def _normalize_booking_type(raw: Optional[str]) -> str:
    if not raw:
        return ""
    return raw.lower().replace(" ", "_").replace("-", "_")


def _normalize_user_gender_for_booking(raw: Optional[Any]) -> Optional[str]:
    """Profiles: male | female only for restriction checks; None if unset/nonstandard."""
    normalized = normalize_user_gender(raw)
    return normalized.value if normalized is not None else None


def _normalize_class_gender_for_booking(raw: Optional[Any]) -> str:
    """
    Classes: mixed → anyone; male/female → only matching members.
    Unknown or empty → treat as mixed (no restriction).
    """
    if raw is None:
        return "mixed"
    s = str(raw).strip().lower()
    if not s:
        return "mixed"
    if s in ("mixed", "any", "all", "both"):
        return "mixed"
    if s in ("male", "men", "man", "m"):
        return "male"
    if s in ("female", "women", "woman", "f"):
        return "female"
    return "mixed"


def _gender_eligibility_message(class_gender: str, user_gender: Optional[str]) -> Tuple[bool, str]:
    if class_gender == "mixed":
        return True, ""
    if user_gender is None:
        return False, "Your profile gender is required to book this class."
    if class_gender == user_gender:
        return True, ""
    if class_gender == "female":
        return False, "This class is for women only."
    if class_gender == "male":
        return False, "This class is for men only."
    return False, "You cannot book this class."


# gym_classes.booking_type values that mean "must book with a package / sale", not wallet or free.
_PACKAGE_ONLY_BOOKING_TYPES = frozenset(
    {
        "packages",
        "package",
        "class_package",
        "package_only",
        "with_package",
    }
)

_PAID_BOOKING_TYPES = frozenset(
    {
        "price",
        "priced",
        "paid",
        "drop_in",
        "dropin",
        "pay_per_class",
        "pay_per_session",
    }
)


def _class_is_package_only(booking_type: Optional[str]) -> bool:
    t = _normalize_booking_type(booking_type)
    return t in _PACKAGE_ONLY_BOOKING_TYPES


def _class_is_paid(gym_class: GymClass) -> bool:
    if _class_price_decimal(gym_class) > 0:
        return True
    return _normalize_booking_type(gym_class.booking_type) in _PAID_BOOKING_TYPES


def _class_price_decimal(gym_class: GymClass) -> Decimal:
    try:
        return Decimal(str(gym_class.price or 0))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal("0")


def _member_booking_amount_fields(
    gym_class: Optional[GymClass],
    gym_config: GymConfigValue,
) -> dict[str, Any]:
    """Paid classes (e.g. booking_type=price) include charge amount on member booking lists."""
    if gym_class is None or not _class_is_paid(gym_class):
        return {}
    price = _class_price_decimal(gym_class)
    if price <= 0:
        return {}
    return {
        "amount": price,
        "currency": gym_config.resolved_currency(),
    }


def _normalize_seat_label(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def _class_has_layout(gym_class: GymClass) -> bool:
    layouts = getattr(gym_class, "layouts", None)
    if layouts not in (None, "", [], {}):
        return True
    lid = gym_class.layout_id
    if lid is None:
        return False
    try:
        return int(lid) != 0
    except (TypeError, ValueError):
        return True


def _layout_total_seats(gym_class: GymClass) -> Optional[int]:
    layouts = getattr(gym_class, "layouts", None)
    if not isinstance(layouts, dict):
        return None
    raw = layouts.get("totalSeats")
    if raw is None:
        # Backward-compatible fallback: some layouts only provide seats[] without totalSeats.
        seats = layouts.get("seats")
        if isinstance(seats, list):
            with_id = [s for s in seats if isinstance(s, dict) and s.get("id") is not None]
            n = len(with_id)
            return n if n > 0 else None
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _effective_capacity(gym_class: GymClass) -> int:
    """
    Final slot capacity for booking checks:
    - layout classes: layouts.totalSeats (if present)
    - fallback: gym_classes.max_bookings
    - <=0 means unlimited
    """
    layout_seats = _layout_total_seats(gym_class) if _class_has_layout(gym_class) else None
    if layout_seats is not None:
        return int(layout_seats)
    return int(gym_class.max_bookings or 0)


def _layout_seat_exists(gym_class: GymClass, seat_id: str) -> Optional[str]:
    """Return an error message when the seat id is not in the class layout."""
    layouts = getattr(gym_class, "layouts", None)
    if layouts in (None, "", [], {}):
        return "Class layout is not configured"
    if not isinstance(layouts, dict):
        return "Invalid class layout format"
    seats = layouts.get("seats")
    if not isinstance(seats, list):
        return "Invalid class layout seats data"
    for seat in seats:
        if not isinstance(seat, dict):
            continue
        if str(seat.get("id")) == seat_id:
            return None
    return "Seat id not found in class layout"


def _layout_seat_status(gym_class: GymClass, seat_id: str) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (status, error). status is lower-cased if seat exists.
    """
    layouts = getattr(gym_class, "layouts", None)
    if layouts in (None, "", [], {}):
        return None, "Class layout is not configured"
    if not isinstance(layouts, dict):
        return None, "Invalid class layout format"
    seats = layouts.get("seats")
    if not isinstance(seats, list):
        return None, "Invalid class layout seats data"
    for seat in seats:
        if not isinstance(seat, dict):
            continue
        if str(seat.get("id")) == seat_id:
            st = seat.get("status")
            return (str(st).lower() if st is not None else None), None
    return None, "Seat id not found in class layout"


def _set_layout_seat_status(gym_class: GymClass, seat_id: str, status_value: str) -> bool:
    """
    Mutates gym_class.layouts seat status in-memory. Caller commits session.
    """
    layouts = getattr(gym_class, "layouts", None)
    if not isinstance(layouts, dict):
        return False
    seats = layouts.get("seats")
    if not isinstance(seats, list):
        return False
    changed = False
    for seat in seats:
        if not isinstance(seat, dict):
            continue
        if str(seat.get("id")) == seat_id:
            seat["status"] = status_value
            changed = True
            break
    if changed:
        gym_class.layouts = layouts
        attributes.flag_modified(gym_class, "layouts")
    return changed


def _sessions_remaining_from_sale(sale: Sale) -> Optional[int]:
    from sqlalchemy.orm import object_session

    db = object_session(sale)
    if db is None:
        return None
    return sessions_remaining_for_sale(db, sale)


def _within_free_cancel_window(
    *,
    starts_at: Optional[datetime],
    now: datetime,
    cancel_hours: int,
) -> bool:
    if starts_at is None:
        return True
    cutoff = starts_at - timedelta(hours=cancel_hours) if cancel_hours > 0 else starts_at
    return now <= cutoff


def _finalize_booking_validation(outcome: "BookingValidationOutcome", payment_mode: str) -> None:
    if outcome.ok:
        ps = outcome.proposed_status or ""
        if payment_mode == "gateway" and ps in ("confirmed", "pending"):
            outcome.proceed_to = "payment"
        elif ps == WAITING_STATUS.value:
            outcome.proceed_to = "waitlist"
        elif ps == "pending_payment":
            outcome.proceed_to = "payment"
        else:
            outcome.proceed_to = "confirm"
        outcome.summary_message = None
        return
    cm = outcome.checks_map
    if payment_mode == "package":
        sess = cm.get("package_sessions") or {}
        if sess.get("pass") is False:
            outcome.proceed_to = "payment_selection"
            outcome.summary_message = sess.get("message") or (
                "Package has 0 sessions remaining. Please choose another payment method."
            )
            return
        pv = cm.get("package_valid") or {}
        if pv.get("pass") is False:
            outcome.proceed_to = "payment_selection"
            outcome.summary_message = pv.get("message")
            return
    adv = cm.get("advance_booking_time") or {}
    if adv.get("pass") is False:
        outcome.proceed_to = None
        outcome.summary_message = adv.get("message")
        return
    cap = cm.get("capacity") or {}
    mw = cm.get("max_waiting_reached") or {}
    if cap.get("pass") is False or mw.get("pass") is False:
        outcome.proceed_to = None
        outcome.summary_message = (
            cap.get("message")
            or mw.get("message")
            or "Cannot book this class right now."
        )
        return
    for _k, v in cm.items():
        if v.get("pass") is False and v.get("message"):
            outcome.summary_message = v["message"]
            break
    outcome.proceed_to = None


@dataclass
class BookingValidationOutcome:
    ok: bool = True
    checks_map: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    gym_class: Optional[GymClass] = None
    proposed_status: Optional[str] = None
    waiting_position: Optional[int] = None
    sale: Optional[Sale] = None
    proceed_to: Optional[str] = None
    summary_message: Optional[str] = None
    # Set after gym_class resolves so create() can reuse without another TenantSetting read.
    gym_config: Optional[GymConfigValue] = None

    def set_check(self, key: str, passed: bool, **extra: Any) -> None:
        body: Dict[str, Any] = {"pass": passed}
        for k, v in extra.items():
            if v is not None:
                body[k] = v
        self.checks_map[key] = body
        if not passed:
            self.ok = False


class BookingsService:
    @staticmethod
    def list_member_bookings(
        db: Session,
        tenant_id: str,
        user: User,
        gym_config: Optional[GymConfigValue] = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        cfg = gym_config if gym_config is not None else GymConfigService.get_gym_config(db, tenant_id)
        tz = _tenant_tz(db, tenant_id, gym_config=cfg)
        now = datetime.now(tz)

        booking_filters = (
            ClassBooking.tenant_id == tenant_id,
            ClassBooking.user_id == user.id,
        )
        total = db.query(ClassBooking).filter(*booking_filters).count()
        offset = (page - 1) * limit
        total_pages = (total + limit - 1) // limit if total else 0

        trainer_user = aliased(User)
        program = aliased(FitnessProgram)
        rows = (
            db.query(ClassBooking, GymClass, trainer_user, program)
            .outerjoin(GymClass, ClassBooking.class_id == GymClass.id)
            .outerjoin(trainer_user, GymClass.trainer_id == trainer_user.id)
            .outerjoin(
                program,
                and_(
                    program.id == GymClass.training_programme_id,
                    program.tenant_id == tenant_id,
                ),
            )
            .filter(*booking_filters)
            .order_by(ClassBooking.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        out: dict[str, Any] = {
            "upcoming": [],
            "past": [],
            "waiting": [],
        }
        for booking, gym_class, trainer, training_program in rows:
            starts_at = _class_starts_at(gym_class, tz) if gym_class is not None else None
            class_name = None
            booking_type: Optional[str] = None
            if gym_class is not None:
                class_name = gym_class.title or gym_class.theme_name
                booking_type = gym_class.booking_type
            trainer_name: Optional[str] = None
            if trainer:
                trainer_name = f"{trainer.first_name or ''} {trainer.last_name or ''}".strip() or trainer.email

            can_cancel, cancel_deadline_iso = booking_cancel_info(
                booking, gym_class, cfg, tz, now
            )

            cancelled_at_iso: Optional[str] = None
            if booking.status == CANCELLED_STATUS and booking.cancelled_at is not None:
                cancelled_at_iso = (
                    booking.cancelled_at.astimezone(dt_timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )

            item: dict[str, Any] = {
                "booking_id": str(booking.id),
                "booking_ref": booking.booking_ref,
                "class_id": str(getattr(gym_class, "id", None) or booking.class_id),
                "class_name": class_name,
                "booking_type": booking_type,
                "status": class_booking_status_value(booking.status),
                "seat_id": booking.seat_id,
                "date": gym_class.class_date.isoformat() if gym_class and gym_class.class_date else None,
                "start_time": gym_class.start_time.strftime("%H:%M") if gym_class and gym_class.start_time else None,
                "end_time": gym_class.end_time.strftime("%H:%M") if gym_class and gym_class.end_time else None,
                "trainer": trainer_name,
                "program": FitnessProgramsService.program_short_payload(training_program),
                "can_cancel": can_cancel,
                "cancel_deadline": cancel_deadline_iso,
                **_member_booking_amount_fields(gym_class, cfg),
            }
            if cancelled_at_iso is not None:
                item["cancelled_at"] = cancelled_at_iso

            if booking.status == WAITING_STATUS:
                item["waiting_position"] = booking.waiting_position
                out["waiting"].append(item)
                continue

            if starts_at is not None and starts_at > now:
                out["upcoming"].append(item)
            else:
                out["past"].append(item)

        page_count = len(out["upcoming"]) + len(out["past"]) + len(out["waiting"])
        out["count"] = page_count
        out["pagination"] = {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
            "has_more": page < total_pages,
        }
        return out

    @staticmethod
    def _promote_next_waiting(
        db: Session,
        tenant_id: str,
        gym_class: GymClass,
        now: datetime,
        gym_config: Optional[GymConfigValue] = None,
        freed_seat_id: Optional[str] = None,
    ) -> Optional[ClassBooking]:
        """
        Promote oldest waiting booking to an occupying status when a slot is freed.
        Returns the promoted booking, if any.

        Waitlist bookings defer seat selection; when a layout class frees a seat,
        assign that seat_id on promotion.
        """
        waiting_booking = (
            db.query(ClassBooking)
            .filter(
                ClassBooking.class_id == gym_class.id,
                ClassBooking.tenant_id == tenant_id,
                ClassBooking.status == WAITING_STATUS,
            )
            .order_by(ClassBooking.booked_at.asc(), ClassBooking.created_at.asc())
            .first()
        )
        if not waiting_booking:
            return None

        cfg = gym_config if gym_config is not None else GymConfigService.get_gym_config(db, tenant_id)
        target_status = (
            ClassBookingStatus.confirmed
            if cfg.booking_settings.auto_confirm_booking
            else ClassBookingStatus.pending
        )
        promoted_status = target_status

        # Gateway booking should go to payment step after promotion.
        if waiting_booking.payment_mode == "gateway" and target_status in (
            ClassBookingStatus.confirmed,
            ClassBookingStatus.pending,
        ):
            promoted_status = ClassBookingStatus.pending_payment
        else:
            promoted_status = target_status

        original_waiting_position = waiting_booking.waiting_position

        waiting_booking.status = promoted_status
        waiting_booking.waiting_position = None
        waiting_booking.promoted_from_waiting_at = now

        occupies_slot = promoted_status in (
            ClassBookingStatus.confirmed,
            ClassBookingStatus.pending,
        )

        if promoted_status == ClassBookingStatus.confirmed:
            waiting_booking.confirmed_at = now

        if occupies_slot:
            gym_class.booking_counts = int(gym_class.booking_counts or 0) + 1

            # Session is deducted at waitlist join; only legacy rows need debit on promote.
            if (
                waiting_booking.payment_mode == "package"
                and waiting_booking.user_package_purchase_id is not None
                and int(waiting_booking.sessions_deducted or 0) == 0
            ):
                package_sale = (
                    db.query(Sale)
                    .filter(
                        Sale.id == waiting_booking.user_package_purchase_id,
                        Sale.tenant_id == tenant_id,
                    )
                    .first()
                )
                if package_sale is not None:
                    remaining = sessions_remaining_for_sale(db, package_sale)
                    if remaining is not None and remaining < 1:
                        waiting_booking.status = WAITING_STATUS
                        waiting_booking.waiting_position = original_waiting_position
                        waiting_booking.promoted_from_waiting_at = None
                        waiting_booking.confirmed_at = None
                        gym_class.booking_counts = max(0, int(gym_class.booking_counts or 0) - 1)
                        return None

                    if remaining is not None:
                        deducted = apply_package_session_debit_for_booking(
                            db,
                            sale=package_sale,
                            booking=waiting_booking,
                            notes="Class booking (promoted from waiting)",
                        )
                        if deducted == 0:
                            waiting_booking.status = WAITING_STATUS
                            waiting_booking.waiting_position = original_waiting_position
                            waiting_booking.promoted_from_waiting_at = None
                            waiting_booking.confirmed_at = None
                            gym_class.booking_counts = max(0, int(gym_class.booking_counts or 0) - 1)
                            return None
                        waiting_booking.sessions_deducted = deducted
                    else:
                        waiting_booking.sessions_deducted = 1
                    db.flush()

            # Waitlist bookings intentionally have no seat until promotion.
            if _class_has_layout(gym_class) and not waiting_booking.seat_id:
                seat_to_assign = _normalize_seat_label(freed_seat_id)
                if seat_to_assign:
                    waiting_booking.seat_id = seat_to_assign
        return waiting_booking

    @staticmethod
    def _load_class_for_tenant(
        db: Session, tenant_id: str, class_id: UUID
    ) -> Optional[GymClass]:
        """
        Class is bookable for this tenant if:
        - no trainer, or trainer belongs to this tenant, OR
        - linked fitness_programs row matches this tenant (handles trainer tenant data bugs).
        """
        return (
            db.query(GymClass)
            .outerjoin(User, GymClass.trainer_id == User.id)
            .outerjoin(
                FitnessProgram,
                and_(
                    FitnessProgram.id == GymClass.training_programme_id,
                    FitnessProgram.tenant_id == tenant_id,
                ),
            )
            .filter(
                GymClass.id == class_id,
                or_(
                    GymClass.trainer_id.is_(None),
                    User.tenant_id == tenant_id,
                    FitnessProgram.id.isnot(None),
                ),
            )
            .first()
        )

    @staticmethod
    def debug_validate_context(
        db: Session,
        *,
        booking_tenant_id: str,
        api_key_tenant_id: Optional[str],
        user: User,
        class_id: UUID,
        outcome: BookingValidationOutcome,
    ) -> dict[str, Any]:
        """
        Diagnosis for Swagger / logs when DEBUG=True: why CLASS_NOT_IN_YOUR_GYM etc.
        """
        # Reuse instance from validate() when it passed tenant filter (avoids duplicate gym_classes SELECT)
        row = outcome.gym_class or db.query(GymClass).filter(GymClass.id == class_id).first()
        trainer_email: Optional[str] = None
        if row and row.trainer_id:
            tu = db.query(User).filter(User.id == row.trainer_id).first()
            if tu:
                trainer_email = tu.email if isinstance(tu.email, str) else None
        programme: Optional[dict[str, Any]] = None
        pid = 0
        if row and row.training_programme_id is not None:
            try:
                pid = int(row.training_programme_id)
            except (TypeError, ValueError):
                pid = 0
        if row and pid != 0:
            fp = db.query(FitnessProgram).filter(FitnessProgram.id == pid).first()
            if fp:
                programme = {
                    "id": fp.id,
                    "name": (fp.name[:80] + "…") if fp.name and len(fp.name) > 80 else fp.name,
                }
            else:
                programme = {"error": "no_fitness_program_row", "training_programme_id": pid}
        elif row:
            programme = {"skipped": "training_programme_id is null or 0"}

        return {
            "hint": "Debug context for booking validation (DEBUG=true only).",
            "user": {"id": str(user.id), "email": user.email},
            "class": (
                None
                if not row
                else {
                    "id": str(row.id),
                    "title": row.title,
                    "trainer_id": str(row.trainer_id) if row.trainer_id else None,
                    "training_programme_id": pid if pid else None,
                }
            ),
            "trainer": (
                None
                if not row or not row.trainer_id
                else {
                    "user_id": str(row.trainer_id),
                    "email": trainer_email,
                }
            ),
            "programme": programme,
            "tenant_filter_load_ok": outcome.gym_class is not None,
            "validation_outcome_ok": outcome.ok,
            "failed_checks": [
                {"code": k, "message": v.get("message")}
                for k, v in outcome.checks_map.items()
                if not v.get("pass")
            ],
        }

    @staticmethod
    def _count_by_statuses(
        db: Session, class_id: UUID, statuses: Sequence[str]
    ) -> int:
        return (
            db.query(func.count(ClassBooking.id))
            .filter(
                ClassBooking.class_id == class_id,
                ClassBooking.status.in_(list(statuses)),
            )
            .scalar()
            or 0
        )

    @staticmethod
    def validate(
        db: Session,
        tenant_id: str,
        user: User,
        class_id: UUID,
        payment_mode: PaymentMode,
        user_package_purchase_id: Optional[UUID],
        seat_id: Optional[str],
        cfg: Optional[GymConfigValue] = None,
    ) -> BookingValidationOutcome:
        outcome = BookingValidationOutcome()
        pm = payment_mode

        if settings.DEBUG:
            logger.info(
                "booking.validate start class_id=%s booking_tenant_id=%s user_id=%s payment=%s",
                class_id,
                tenant_id,
                user.id,
                payment_mode,
            )

        if str(user.tenant_id) != str(tenant_id):
            outcome.set_check(
                "tenant_user",
                False,
                message="User does not belong to this tenant",
            )
            _finalize_booking_validation(outcome, pm)
            return outcome

        by_id = db.query(GymClass).filter(GymClass.id == class_id).first()
        if not by_id:
            outcome.set_check(
                "class_exists",
                False,
                message="No class exists with this id — check the UUID in the URL",
            )
            outcome.set_check("class_in_your_gym", False, message="—")
            _finalize_booking_validation(outcome, pm)
            return outcome

        outcome.set_check("class_exists", True)

        gym_class = BookingsService._load_class_for_tenant(db, tenant_id, class_id)
        if not gym_class:
            outcome.set_check(
                "class_in_your_gym",
                False,
                message=(
                    "This class is not bookable for your gym: trainer or training programme "
                    "must belong to your tenant."
                ),
            )
            _finalize_booking_validation(outcome, pm)
            return outcome

        outcome.set_check("class_in_your_gym", True)
        outcome.gym_class = gym_class

        config = cfg if cfg is not None else GymConfigService.get_gym_config(db, tenant_id)
        outcome.gym_config = config
        tz = _tenant_tz(db, tenant_id, gym_config=config)
        now = datetime.now(tz)
        starts_at = _class_starts_at(gym_class, tz)

        # Class must be active and not cancelled.
        if _is_cancelled_class(getattr(gym_class, "status", None)):
            outcome.set_check("class_active", False, message="Class is cancelled")
            _finalize_booking_validation(outcome, pm)
            return outcome
        if _is_inactive_class(getattr(gym_class, "status", None)):
            outcome.set_check("class_active", False, message="Class is not active")
            _finalize_booking_validation(outcome, pm)
            return outcome
        if (str(getattr(gym_class, "status", "") or "").strip().lower() == "draft"):
            pub = getattr(gym_class, "publish_at", None)
            if pub is None:
                outcome.set_check("class_active", False, message="Class is not published yet")
                _finalize_booking_validation(outcome, pm)
                return outcome
            pub_aware = pub if pub.tzinfo is not None else pub.replace(tzinfo=dt_timezone.utc)
            if pub_aware > datetime.now(dt_timezone.utc):
                outcome.set_check("class_active", False, message="Class is not published yet")
                _finalize_booking_validation(outcome, pm)
                return outcome
        outcome.set_check("class_active", True)

        cg = _normalize_class_gender_for_booking(getattr(gym_class, "gender", None))
        ug = _normalize_user_gender_for_booking(getattr(user, "gender", None))
        gender_ok, gender_msg = _gender_eligibility_message(cg, ug)
        outcome.set_check(
            "gender_eligibility",
            gender_ok,
            message=(gender_msg or None),
            class_gender=cg,
            user_gender=(ug if ug is not None else None),
        )
        if not gender_ok:
            _finalize_booking_validation(outcome, pm)
            return outcome

        if starts_at and starts_at <= now:
            outcome.set_check("class_not_started", False, message="Class has already started")
            _finalize_booking_validation(outcome, pm)
            return outcome
        outcome.set_check("class_not_started", True)

        # Booking cutoff: disallow bookings too close to start time.
        cutoff_mins = int(getattr(config.booking_settings, "booking_cutoff_minutes", 0) or 0)
        if cutoff_mins > 0 and starts_at is not None:
            cutoff_at = starts_at - timedelta(minutes=cutoff_mins)
            if now > cutoff_at:
                outcome.set_check(
                    "booking_cutoff_time",
                    False,
                    message=f"Booking is closed {cutoff_mins} minutes before class start",
                )
                _finalize_booking_validation(outcome, pm)
                return outcome
        outcome.set_check("booking_cutoff_time", True)

        # Class billing mode: package-type → package only; price > 0 / paid type → wallet/gateway; else → free only.
        pkg_only = _class_is_package_only(gym_class.booking_type)
        is_paid = _class_is_paid(gym_class)
        if pkg_only:
            allowed_pm: frozenset[str] = frozenset({"package"})
        elif is_paid:
            allowed_pm = frozenset({"wallet", "gateway", "cash"})
        else:
            allowed_pm = frozenset({"free"})

        if pm not in allowed_pm:
            if pkg_only:
                pay_msg = "This class is package-only — use payment_mode package with a valid package sale."
            elif is_paid:
                pay_msg = (
                    "This class has a price — book with wallet or gateway, not free or package."
                )
            else:
                pay_msg = "This class is free — use payment_mode free."
            outcome.set_check(
                "class_payment_mode",
                False,
                message=pay_msg,
                allowed=list(allowed_pm),
            )
            _finalize_booking_validation(outcome, pm)
            return outcome

        adv = config.booking_settings.advance_booking_window_days
        if adv and gym_class.class_date:
            last_bookable = now.date().fromordinal(now.date().toordinal() + int(adv))
            if gym_class.class_date > last_bookable:
                outcome.set_check(
                    "advance_booking_time",
                    False,
                    opens_at=None,
                    message=f"Class is outside the advance booking window ({adv} days ahead).",
                )
            else:
                outcome.set_check(
                    "advance_booking_time",
                    True,
                    opens_at=None,
                    message="Within advance booking window",
                )
        else:
            outcome.set_check("advance_booking_time", True, opens_at=None)

        outcome.set_check("min_booking_time", True, message="No minimum lead-time rule configured")

        dup = (
            db.query(ClassBooking)
            .filter(
                ClassBooking.class_id == class_id,
                ClassBooking.user_id == user.id,
                ClassBooking.status.in_(list(ACTIVE_USER_BOOKING_STATUSES)),
            )
            .first()
        )
        if dup:
            outcome.set_check(
                "already_booked",
                False,
                message="You already have an active booking for this class",
            )
        else:
            outcome.set_check("already_booked", True)

        occupying = BookingsService._count_by_statuses(db, class_id, OCCUPYING_SLOT_STATUSES)
        waiting_n = BookingsService._count_by_statuses(db, class_id, (WAITING_STATUS,))
        max_bookings = _effective_capacity(gym_class)
        max_waitings = int(gym_class.max_waitings or 0)

        has_slot = max_bookings <= 0 or occupying < max_bookings
        waitlist_ok = (
            not has_slot
            and config.booking_settings.allow_waiting_list
            and max_waitings > 0
            and waiting_n < max_waitings
        )
        seats_left: Optional[int] = None
        if max_bookings > 0:
            seats_left = max(0, max_bookings - occupying)

        can_book = has_slot or waitlist_ok
        if can_book:
            outcome.set_check("capacity", True, seats_left=seats_left)
            outcome.set_check("max_waiting_reached", True)
            if max_bookings == 1 and occupying >= 1 and waitlist_ok:
                outcome.set_check("one_to_one_available", True)
        else:
            if max_bookings == 1 and occupying >= 1:
                detail = "This class is already booked by another user"
            else:
                detail = "Class is full"
            if not config.booking_settings.allow_waiting_list:
                detail += " and waiting list is disabled"
            elif max_waitings <= 0:
                detail += " and waiting list is not configured"
            else:
                detail += " and waiting list is full"
            outcome.set_check(
                "capacity",
                False,
                seats_left=max(0, seats_left if seats_left is not None else 0),
                message=detail,
            )
            outcome.set_check("max_waiting_reached", False, message=detail)
            if max_bookings == 1 and occupying >= 1:
                outcome.set_check("one_to_one_available", False, message=detail)

        if pm == "free":
            fe = config.payment_pricing.enable_free_classes
            outcome.set_check(
                "free_booking",
                fe,
                message=None if fe else "Free class booking is disabled",
            )
        elif pm == "wallet":
            if not config.payment_pricing.enable_pay_per_class:
                outcome.set_check(
                    "wallet_balance",
                    False,
                    message="Pay-per-class (wallet) is disabled",
                )
            else:
                price = Decimal(str(gym_class.price or 0))
                bal = Decimal(str(user.wallet or 0))
                ok_wb = price <= 0 or bal >= price
                outcome.set_check(
                    "wallet_balance",
                    ok_wb,
                    message=None if ok_wb else "Insufficient wallet balance for this class",
                )
        elif pm == "gateway":
            ge = config.payment_pricing.enable_pay_per_class
            outcome.set_check(
                "gateway_pay",
                ge,
                message=None if ge else "Pay-per-class (gateway) is disabled",
            )

        if pm == "package":
            sale: Optional[Sale] = None
            if not config.payment_pricing.enable_class_package:
                outcome.set_check("package_valid", False, message="Package booking is disabled")
            elif not user_package_purchase_id:
                outcome.set_check(
                    "package_valid",
                    False,
                    message="Package purchase (sale id) is required",
                )
            else:
                sale = (
                    db.query(Sale)
                    .filter(
                        Sale.id == user_package_purchase_id,
                        Sale.tenant_id == tenant_id,
                        Sale.user_id == user.id,
                        (
                            Sale.type.in_(["package_gateway", "package_wallet"])
                            | ((Sale.type == "gateway") & (Sale.product_item_type == "package"))
                            | ((Sale.type == "wallet") & (Sale.product_item_type == "package"))
                        ),
                        Sale.package_id.isnot(None),
                        sale_succeeded_clause(),
                    )
                    .first()
                )
                if not sale:
                    outcome.set_check(
                        "package_valid",
                        False,
                        message="Invalid package purchase or payment not completed",
                    )
                else:
                    outcome.set_check("package_valid", True)
                    outcome.sale = sale

            expires_at_str: Optional[str] = None
            rem: Optional[int] = None
            if sale:
                ex = sale_expires_at(db, sale)
                expired = False
                if ex is not None:
                    ex_aware = ex if ex.tzinfo is not None else ex.replace(tzinfo=dt_timezone.utc)
                    if ex_aware <= datetime.now(dt_timezone.utc):
                        expired = True
                    else:
                        expires_at_str = ex_aware.date().isoformat()
                if expired:
                    outcome.set_check(
                        "package_not_expired",
                        False,
                        expires_at=None,
                        message="Package purchase has expired",
                    )
                else:
                    outcome.set_check(
                        "package_not_expired",
                        True,
                        expires_at=expires_at_str,
                    )
                rem = sessions_remaining_for_sale(db, sale)
                if rem is not None and rem < 1:
                    outcome.set_check(
                        "package_sessions",
                        False,
                        remaining=0,
                        message="No sessions left on this package",
                    )
                else:
                    outcome.set_check(
                        "package_sessions",
                        True,
                        remaining=rem,
                    )
            else:
                outcome.set_check(
                    "package_not_expired",
                    False,
                    expires_at=None,
                    message="Invalid package purchase or payment not completed",
                )
                outcome.set_check(
                    "package_sessions",
                    False,
                    remaining=0,
                    message="Invalid package purchase or payment not completed",
                )

            outcome.set_check("package_location", True)
            outcome.set_check("package_time_slot", True)
            outcome.set_check("package_program", True)
            outcome.set_check("one_time_package_reuse", True)
        else:
            outcome.set_check("package_valid", True)
            outcome.set_check("package_sessions", True, remaining=None)
            outcome.set_check("package_not_expired", True, expires_at=None)
            outcome.set_check("package_location", True)
            outcome.set_check("package_time_slot", True)
            outcome.set_check("package_program", True)
            outcome.set_check("one_time_package_reuse", True)

        has_layout = _class_has_layout(gym_class)
        seat_label = _normalize_seat_label(seat_id)
        if has_layout:
            if not seat_label:
                # Seat is required only when user is getting a real slot. If booking goes to waitlist,
                # seat selection is deferred until promotion.
                if has_slot:
                    outcome.set_check(
                        "seat_selection",
                        False,
                        message='This class has a layout — send seat_id as the seat label (e.g. "A1").',
                    )
                else:
                    outcome.set_check(
                        "seat_selection",
                        True,
                        message="Seat selection not required for waitlist booking",
                    )
            elif not has_slot:
                outcome.set_check(
                    "seat_selection",
                    True,
                    message="Seat selection not required for waitlist booking",
                )
            else:
                seat_err = _layout_seat_exists(gym_class, seat_label)
                if seat_err:
                    outcome.set_check("seat_selection", False, message=seat_err)
                    _finalize_booking_validation(outcome, pm)
                    return outcome
                taken = (
                    db.query(ClassBooking)
                    .filter(
                        ClassBooking.class_id == class_id,
                        cast(ClassBooking.seat_id, SAString) == seat_label,
                        ClassBooking.status.in_(list(ACTIVE_USER_BOOKING_STATUSES)),
                    )
                    .first()
                )
                if taken:
                    outcome.set_check(
                        "seat_selection",
                        False,
                        message="This seat is already taken",
                    )
                else:
                    outcome.set_check("seat_selection", True, seat_id=seat_label)
        else:
            if seat_label:
                outcome.set_check(
                    "seat_selection",
                    False,
                    message="This class has no layout — omit seat_id.",
                )
            else:
                outcome.set_check(
                    "seat_selection",
                    True,
                    message="Seat not required (class has no layout configured)",
                )

        if outcome.ok:
            if has_slot:
                # One-to-one bookings should always be confirmed when a slot is available.
                if max_bookings == 1:
                    proposed = "confirmed"
                else:
                    proposed = "confirmed" if config.booking_settings.auto_confirm_booking else "pending"
            else:
                proposed = WAITING_STATUS.value
            outcome.proposed_status = proposed
            if proposed == WAITING_STATUS.value:
                outcome.waiting_position = waiting_n + 1

        _finalize_booking_validation(outcome, pm)
        return outcome

    @staticmethod
    def create(
        db: Session,
        tenant_id: str,
        user: User,
        class_id: UUID,
        payment_mode: PaymentMode,
        user_package_purchase_id: Optional[UUID],
        seat_id: Optional[str],
        notes: Optional[str],
        force_waiting: bool = False,
        gym_config: Optional[GymConfigValue] = None,
    ) -> Tuple[ClassBooking, Optional[UUID]]:
        outcome = BookingsService.validate(
            db,
            tenant_id,
            user,
            class_id,
            payment_mode,
            user_package_purchase_id,
            seat_id,
            cfg=gym_config,
        )
        if not outcome.ok or not outcome.gym_class or not outcome.proposed_status:
            msg = outcome.summary_message or "Booking validation failed"
            if not msg or msg == "Booking validation failed":
                for _k, v in outcome.checks_map.items():
                    if not v.get("pass") and v.get("message"):
                        msg = v["message"]
                        break
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
        if force_waiting and outcome.proposed_status != WAITING_STATUS.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Class has available slot; use regular booking API.",
            )

        gym_class = outcome.gym_class
        status_str = outcome.proposed_status
        if payment_mode == "gateway" and status_str in ("confirmed", "pending"):
            status_str = "pending_payment"

        # Resolve timezone before persisting the booking.
        now = datetime.now(_tenant_tz(db, tenant_id, gym_config=outcome.gym_config))

        sessions_deducted = 0
        wallet_txn_id: Optional[UUID] = None
        sale_id: Optional[UUID] = None
        resolved_package_id: Optional[UUID] = None
        package_sale: Optional[Sale] = None

        if payment_mode == "package" and user_package_purchase_id:
            sale_id = user_package_purchase_id
            package_sale = (
                db.query(Sale)
                .filter(
                    Sale.id == sale_id,
                    Sale.tenant_id == tenant_id,
                    Sale.user_id == user.id,
                )
                .first()
            )
            if not package_sale:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Package purchase not found",
                )
            resolved_package_id = package_sale.package_id
            if status_str in ("confirmed", WAITING_STATUS.value):
                rem = sessions_remaining_for_sale(db, package_sale)
                if rem is not None and rem < 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="No sessions left on this package",
                    )

        if payment_mode == "wallet":
            price = Decimal(str(gym_class.price or 0))
            # Charge upfront whenever the booking is created (confirmed, pending, or waitlist).
            if price > 0 and status_str in _WALLET_CHARGE_STATUS_VALUES:
                db_user = _wallet_user(db, user)
                bal_before = Decimal(str(db_user.wallet or 0))
                if bal_before < price:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Insufficient wallet balance",
                    )
                bal_after = bal_before - price
                txn = WalletTransaction(
                    user_id=db_user.id,
                    direction="debit",
                    transaction_id=None,
                    amount=price,
                    currency=(
                        outcome.gym_config.resolved_currency()
                        if outcome.gym_config
                        else GymConfigService.get_currency(db, tenant_id)
                    ),
                    balance_before=bal_before,
                    balance_after=bal_after,
                    created_by=db_user.user_type or "member",
                    created_by_id=db_user.id,
                )
                db.add(txn)
                db.flush()
                wallet_txn_id = txn.id
                db_user.wallet = bal_after

        booking_status = normalize_class_booking_status(status_str)
        seat_label_for_booking = _normalize_seat_label(seat_id)
        if booking_status not in OCCUPYING_SLOT_STATUSES:
            seat_label_for_booking = None
        booking = ClassBooking(
            tenant_id=tenant_id,
            user_id=user.id,
            class_id=class_id,
            location_id=_location_id_for_class(db, gym_class, tenant_id),
            seat_id=seat_label_for_booking,
            status=booking_status,
            waiting_position=outcome.waiting_position if booking_status == WAITING_STATUS else None,
            booked_at=now,
            confirmed_at=now if booking_status == ClassBookingStatus.confirmed else None,
            payment_mode=payment_mode,
            user_package_purchase_id=sale_id,
            package_id=resolved_package_id,
            sessions_deducted=sessions_deducted,
            notes=notes,
        )
        db.add(booking)
        db.flush()
        # booking_ref is assigned by ClassBooking.before_insert (Snowflake BK-*).
        # Keep audit marker in notes (DB no longer stores wallet_txn_id on booking).
        if wallet_txn_id is not None:
            booking.notes = _append_bfy_wtxn_note(booking.notes, wallet_txn_id, "debit")
            db.flush()

        if (
            payment_mode == "package"
            and package_sale is not None
            and booking_status in (ClassBookingStatus.confirmed, WAITING_STATUS)
        ):
            debit_notes = (
                "Class booking (waitlist)"
                if booking_status == WAITING_STATUS
                else "Class booking"
            )
            remaining = sessions_remaining_for_sale(db, package_sale)
            if remaining is not None:
                deducted = apply_package_session_debit_for_booking(
                    db,
                    sale=package_sale,
                    booking=booking,
                    notes=debit_notes,
                )
                if deducted == 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Failed to deduct package session",
                    )
                booking.sessions_deducted = deducted
            else:
                booking.sessions_deducted = 1
            db.flush()

        if booking_status == ClassBookingStatus.confirmed:
            cap = _effective_capacity(gym_class)
            current_count = int(gym_class.booking_counts or 0)
            if cap > 0 and current_count >= cap:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Class is full",
                )
            gym_class.booking_counts = (
                min(cap, current_count + 1) if cap > 0 else current_count + 1
            )

        return booking, wallet_txn_id

    @staticmethod
    def cancel(
        db: Session,
        tenant_id: str,
        user: User,
        class_id: UUID,
        booking_id: UUID,
        reason: Optional[str],
        gym_config: Optional[GymConfigValue] = None,
    ) -> Tuple[ClassBooking, Optional[ClassBooking]]:
        booking = (
            db.query(ClassBooking)
            .filter(
                ClassBooking.id == booking_id,
                ClassBooking.class_id == class_id,
                ClassBooking.tenant_id == tenant_id,
                ClassBooking.user_id == user.id,
            )
            .first()
        )
        if not booking:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")

        if booking.status in (CANCELLED_STATUS, ClassBookingStatus.completed):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Booking already {class_booking_status_value(booking.status)}",
            )

        gym_class = (
            db.query(GymClass)
            .filter(GymClass.id == class_id)
            .first()
        )
        if not gym_class:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found")

        cfg = gym_config if gym_config is not None else GymConfigService.get_gym_config(db, tenant_id)
        tz = _tenant_tz(db, tenant_id, gym_config=cfg)
        now = datetime.now(tz)
        starts_at = _class_starts_at(gym_class, tz)

        if starts_at is not None:
            if not cfg.booking_settings.allow_late_cancellations:
                if starts_at <= now:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Cancellation window closed: class already started",
                    )
                cancel_hours = int(cfg.booking_settings.cancellation_window_hours or 0)
                if cancel_hours > 0:
                    cutoff = starts_at - timedelta(hours=cancel_hours)
                    if now > cutoff:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=(
                                f"Cancellation allowed only {cancel_hours}h before class start"
                            ),
                        )

        previous_status = booking.status
        seat_label = _normalize_seat_label(booking.seat_id)

        within_free_window = True
        cancel_hours = int(cfg.booking_settings.cancellation_window_hours or 0)
        if starts_at is not None:
            within_free_window = _within_free_cancel_window(
                starts_at=starts_at,
                now=now,
                cancel_hours=cancel_hours,
            )

        package_sale: Optional[Sale] = None
        user_package = None
        if (
            booking.payment_mode == "package"
            and booking.user_package_purchase_id is not None
            and int(booking.sessions_deducted or 0) > 0
        ):
            package_sale = (
                db.query(Sale)
                .filter(
                    Sale.id == booking.user_package_purchase_id,
                    Sale.tenant_id == tenant_id,
                    Sale.user_id == user.id,
                )
                .first()
            )
            if package_sale is not None:
                user_package = get_user_package_for_sale(db, package_sale.id)

        promoted_booking: Optional[ClassBooking] = None
        tx_ctx = db.begin_nested() if db.in_transaction() else db.begin()
        with tx_ctx:
            if package_sale is not None and user_package is not None:
                sessions_to_restore = int(booking.sessions_deducted or 0)
                if previous_status == WAITING_STATUS:
                    record_booking_refund_credit(
                        db,
                        user_package=user_package,
                        sale=package_sale,
                        booking=booking,
                        sessions=sessions_to_restore,
                        notes="Waitlist cancelled — session refunded",
                    )
                elif within_free_window:
                    record_booking_refund_credit(
                        db,
                        user_package=user_package,
                        sale=package_sale,
                        booking=booking,
                        sessions=sessions_to_restore,
                    )
                else:
                    record_late_cancel_audit(
                        db,
                        user_package=user_package,
                        booking=booking,
                    )

            booking.status = CANCELLED_STATUS
            booking.cancelled_at = now
            booking.cancelled_by_user_id = user.id
            booking.cancellation_reason = (reason or "").strip() or None

            _refund_wallet_for_cancelled_booking(
                db,
                user=user,
                booking=booking,
                gym_class=gym_class,
                within_free_window=within_free_window,
            )

            if previous_status == ClassBookingStatus.confirmed:
                gym_class.booking_counts = max(0, int(gym_class.booking_counts or 0) - 1)
                promoted_booking = BookingsService._promote_next_waiting(
                    db,
                    tenant_id,
                    gym_class,
                    now,
                    gym_config=cfg,
                    freed_seat_id=seat_label,
                )

        db.refresh(booking)
        if promoted_booking is not None:
            db.refresh(promoted_booking)
        return booking, promoted_booking
