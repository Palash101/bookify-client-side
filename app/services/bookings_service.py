from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as time_type, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Any, Optional, Sequence, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import String as SAString, and_, cast, func, or_
from sqlalchemy.orm import Session, aliased, attributes

from app.models.class_booking import ClassBooking
from app.models.fitness_program import FitnessProgram
from app.models.gym_class import GymClass
from app.models.sales import Sale
from app.models.tenant import Tenant
from app.models.user import User
from app.models.wallet_transactions import WalletTransaction
from fastapi import HTTPException, status

from app.core.settings import settings
from app.schemas.booking import PaymentMode
from app.schemas.gym_config_value import GymConfigValue
from app.services.gym_config_service import GymConfigService

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


COMMON_TZ_ABBREVS = {
    "IST": "Asia/Kolkata",
    "GST": "Asia/Dubai",
    "QAT": "Asia/Qatar",
    "AST": "Asia/Riyadh",
    "PKT": "Asia/Karachi",
    "UTC": "UTC",
}

# Bookings that block the user from booking the same class again
ACTIVE_USER_BOOKING_STATUSES: Tuple[str, ...] = (
    "confirmed",
    "waiting",
    "pending",
    "pending_payment",
)

# Statuses that hold a regular slot (not waitlist).
OCCUPYING_SLOT_STATUSES: Tuple[str, ...] = ("confirmed", "pending", "pending_payment")

WAITING_STATUS = "waiting"
CANCELLED_STATUS = "cancelled"


def _tenant_tz(db: Session, tenant_id: UUID) -> ZoneInfo:
    tenant: Optional[Tenant] = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    tz_name = (tenant.timezone or "UTC").strip() if tenant else "UTC"
    tz_key = tz_name.upper()
    if tz_key in COMMON_TZ_ABBREVS:
        tz_name = COMMON_TZ_ABBREVS[tz_key]
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _class_starts_at(gym_class: GymClass, tz: ZoneInfo) -> Optional[datetime]:
    if not gym_class.class_date or not gym_class.start_time:
        return None
    d: date = gym_class.class_date
    t: time_type = gym_class.start_time
    return datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=tz)


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


def _class_is_package_only(booking_type: Optional[str]) -> bool:
    t = _normalize_booking_type(booking_type)
    return t in _PACKAGE_ONLY_BOOKING_TYPES


def _class_price_decimal(gym_class: GymClass) -> Decimal:
    try:
        return Decimal(str(gym_class.price or 0))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal("0")


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
    meta = sale.extra_metadata or {}
    if not isinstance(meta, dict):
        return None
    # Accept multiple historical key names for compatibility.
    for key in ("sessions_remaining", "remaining_sessions", "remaining_session", "sessions_left"):
        if key not in meta:
            continue
        v = meta[key]
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _restore_sessions_to_sale(sale: Sale, qty: int) -> None:
    if qty <= 0:
        return
    rem = _sessions_remaining_from_sale(sale)
    if rem is None:
        return
    meta: dict[str, Any] = dict(sale.extra_metadata or {})
    meta["sessions_remaining"] = max(0, rem + qty)
    sale.extra_metadata = meta
    attributes.flag_modified(sale, "extra_metadata")


def _finalize_booking_validation(outcome: "BookingValidationOutcome", payment_mode: str) -> None:
    if outcome.ok:
        ps = outcome.proposed_status or ""
        if payment_mode == "gateway" and ps in ("confirmed", "pending"):
            outcome.proceed_to = "payment"
        elif ps == WAITING_STATUS:
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
        tenant_id: UUID,
        user: User,
    ) -> dict[str, list[dict[str, Any]]]:
        tz = _tenant_tz(db, tenant_id)
        now = datetime.now(tz)
        cfg = GymConfigService.get_gym_config(db, tenant_id)
        cancel_hours = int(cfg.booking_settings.cancellation_window_hours or 0)
        allow_late = bool(cfg.booking_settings.allow_late_cancellations)

        trainer_user = aliased(User)
        rows = (
            db.query(ClassBooking, GymClass, trainer_user)
            .join(GymClass, ClassBooking.class_id == GymClass.id)
            .outerjoin(trainer_user, GymClass.trainer_id == trainer_user.id)
            .filter(
                ClassBooking.tenant_id == tenant_id,
                ClassBooking.user_id == user.id,
            )
            .order_by(GymClass.class_date.desc(), GymClass.start_time.desc(), ClassBooking.created_at.desc())
            .all()
        )

        out: dict[str, list[dict[str, Any]]] = {
            "upcoming": [],
            "past": [],
            "waiting": [],
        }
        for booking, gym_class, trainer in rows:
            starts_at = _class_starts_at(gym_class, tz)
            class_name = gym_class.title or gym_class.theme_name
            trainer_name: Optional[str] = None
            if trainer:
                trainer_name = f"{trainer.first_name or ''} {trainer.last_name or ''}".strip() or trainer.email

            if booking.status == WAITING_STATUS:
                out["waiting"].append(
                    {
                        "booking_id": str(booking.id),
                        "order_id": booking.order_id,
                        "class_name": class_name,
                        "status": booking.status,
                        "waiting_position": booking.waiting_position,
                    }
                )
                continue

            cancel_deadline_iso: Optional[str] = None
            can_cancel = False
            if booking.status != CANCELLED_STATUS and starts_at is not None:
                cutoff = starts_at - timedelta(hours=cancel_hours) if cancel_hours > 0 else starts_at
                cancel_deadline_iso = cutoff.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")
                if allow_late:
                    can_cancel = booking.status not in ("completed",)
                else:
                    can_cancel = now <= cutoff and booking.status not in ("completed",)

            cancelled_at_iso: Optional[str] = None
            if booking.status == CANCELLED_STATUS and booking.cancelled_at is not None:
                cancelled_at_iso = (
                    booking.cancelled_at.astimezone(dt_timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )

            item: dict[str, Any] = {
                "booking_id": str(booking.id),
                "order_id": booking.order_id,
                "class_id": str(gym_class.id),
                "class_name": class_name,
                "status": booking.status,
                "seat_id": booking.seat_id,
                "date": gym_class.class_date.isoformat() if gym_class.class_date else None,
                "start_time": gym_class.start_time.strftime("%H:%M") if gym_class.start_time else None,
                "end_time": gym_class.end_time.strftime("%H:%M") if gym_class.end_time else None,
                "trainer": trainer_name,
                "can_cancel": can_cancel,
                "cancel_deadline": cancel_deadline_iso,
            }
            if cancelled_at_iso is not None:
                item["cancelled_at"] = cancelled_at_iso

            if starts_at is not None and starts_at > now:
                out["upcoming"].append(item)
            else:
                out["past"].append(item)
        return out

    @staticmethod
    def _promote_next_waiting(
        db: Session,
        tenant_id: UUID,
        gym_class: GymClass,
        now: datetime,
    ) -> None:
        """
        Promote oldest waiting booking to an occupying status when a slot is freed.
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
            return

        cfg = GymConfigService.get_gym_config(db, tenant_id)
        target_status = "confirmed" if cfg.booking_settings.auto_confirm_booking else "pending"
        promoted_status = target_status

        # Gateway booking should go to payment step after promotion.
        if waiting_booking.payment_mode == "gateway" and target_status in ("confirmed", "pending"):
            promoted_status = "pending_payment"

        waiting_booking.status = promoted_status
        waiting_booking.waiting_position = None
        waiting_booking.promoted_from_waiting_at = now
        if promoted_status == "confirmed":
            waiting_booking.confirmed_at = now
            gym_class.booking_counts = int(gym_class.booking_counts or 0) + 1

    @staticmethod
    def _load_class_for_tenant(
        db: Session, tenant_id: UUID, class_id: UUID
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
        booking_tenant_id: UUID,
        api_key_tenant_id: Optional[UUID],
        user: User,
        class_id: UUID,
        outcome: BookingValidationOutcome,
    ) -> dict[str, Any]:
        """
        Diagnosis for Swagger / logs when DEBUG=True: why CLASS_NOT_IN_YOUR_GYM etc.
        """
        booking_tid_s = str(booking_tenant_id)
        # Reuse instance from validate() when it passed tenant filter (avoids duplicate gym_classes SELECT)
        row = outcome.gym_class or db.query(GymClass).filter(GymClass.id == class_id).first()
        trainer_tid: Optional[str] = None
        trainer_email: Optional[str] = None
        if row and row.trainer_id:
            tu = db.query(User).filter(User.id == row.trainer_id).first()
            if tu:
                trainer_tid = str(tu.tenant_id)
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
                fp_tid = str(fp.tenant_id)
                programme = {
                    "id": fp.id,
                    "tenant_id": fp_tid,
                    "name": (fp.name[:80] + "…") if fp.name and len(fp.name) > 80 else fp.name,
                    "matches_booking_tenant": fp_tid == booking_tid_s,
                }
            else:
                programme = {"error": "no_fitness_program_row", "training_programme_id": pid}
        elif row:
            programme = {"skipped": "training_programme_id is null or 0"}

        api_tid = str(api_key_tenant_id) if api_key_tenant_id else None
        return {
            "hint": "booking_tenant_id = JWT user's users.tenant_id; api_key_tenant = gym from X-Tenant-Key.",
            "booking_tenant_id": booking_tid_s,
            "api_key_tenant_id": api_tid,
            "api_key_matches_user_tenant": api_tid == booking_tid_s if api_tid else None,
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
                    "tenant_id": trainer_tid,
                    "email": trainer_email,
                    "matches_booking_tenant": trainer_tid == booking_tid_s if trainer_tid else None,
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
        tenant_id: UUID,
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

        if UUID(str(user.tenant_id)) != UUID(str(tenant_id)):
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
        tz = _tenant_tz(db, tenant_id)
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

        # Class billing mode: package-type → package only; price > 0 → wallet/gateway; else → free only.
        pkg_only = _class_is_package_only(gym_class.booking_type)
        class_price = _class_price_decimal(gym_class)
        is_paid = class_price > 0
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

        # One-to-one classes (capacity=1) cannot be double-booked or waitlisted.
        if max_bookings == 1 and occupying >= 1:
            outcome.set_check(
                "one_to_one_available",
                False,
                message="This class is already booked by another user",
            )
            _finalize_booking_validation(outcome, pm)
            return outcome

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
                        Sale.type.in_(["package_gateway", "package_wallet"]),
                        Sale.package_id.isnot(None),
                        Sale.status.in_(["succeeded", "success"]),
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
                ex = sale.expires_at
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
                rem = _sessions_remaining_from_sale(sale)
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
                    message="No valid package sale",
                )
                outcome.set_check(
                    "package_sessions",
                    False,
                    remaining=0,
                    message="No valid package sale",
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
            else:
                seat_status, seat_err = _layout_seat_status(gym_class, seat_label)
                if seat_err:
                    outcome.set_check("seat_selection", False, message=seat_err)
                    _finalize_booking_validation(outcome, pm)
                    return outcome
                if seat_status and seat_status != "available":
                    outcome.set_check(
                        "seat_selection",
                        False,
                        message=f"Seat {seat_label} is not available",
                    )
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
                proposed = WAITING_STATUS
            outcome.proposed_status = proposed
            if proposed == WAITING_STATUS:
                outcome.waiting_position = waiting_n + 1

        _finalize_booking_validation(outcome, pm)
        return outcome

    @staticmethod
    def create(
        db: Session,
        tenant_id: UUID,
        user: User,
        class_id: UUID,
        payment_mode: PaymentMode,
        user_package_purchase_id: Optional[UUID],
        seat_id: Optional[str],
        notes: Optional[str],
        force_waiting: bool = False,
    ) -> ClassBooking:
        outcome = BookingsService.validate(
            db,
            tenant_id,
            user,
            class_id,
            payment_mode,
            user_package_purchase_id,
            seat_id,
        )
        if not outcome.ok or not outcome.gym_class or not outcome.proposed_status:
            msg = outcome.summary_message or "Booking validation failed"
            if not msg or msg == "Booking validation failed":
                for _k, v in outcome.checks_map.items():
                    if not v.get("pass") and v.get("message"):
                        msg = v["message"]
                        break
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg)
        if force_waiting and outcome.proposed_status != WAITING_STATUS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Class has available slot; use regular booking API.",
            )

        gym_class = outcome.gym_class
        status_str = outcome.proposed_status
        if payment_mode == "gateway" and status_str in ("confirmed", "pending"):
            status_str = "pending_payment"

        tx_ctx = db.begin_nested() if db.in_transaction() else db.begin()
        with tx_ctx:
            now = datetime.now(_tenant_tz(db, tenant_id))
            sessions_deducted = 0
            wallet_txn_id: Optional[UUID] = None
            sale_id: Optional[UUID] = None

            if payment_mode == "package" and outcome.sale:
                sale = outcome.sale
                sale_id = sale.id
                if status_str in ("confirmed", "waiting"):
                    sessions_deducted = 1
                    rem = _sessions_remaining_from_sale(sale)
                    if rem is not None:
                        meta: dict[str, Any] = dict(sale.extra_metadata or {})
                        meta["sessions_remaining"] = max(0, rem - 1)
                        sale.extra_metadata = meta
                        attributes.flag_modified(sale, "extra_metadata")

            if payment_mode == "wallet":
                price = Decimal(str(gym_class.price or 0))
                # Charge upfront for confirmed + waiting bookings.
                if price > 0 and status_str in ("confirmed", WAITING_STATUS):
                    bal_before = Decimal(str(user.wallet or 0))
                    if bal_before < price:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Insufficient wallet balance",
                        )
                    bal_after = bal_before - price
                    txn = WalletTransaction(
                        user_id=user.id,
                        direction="debit",
                        transaction_id=None,
                        amount=price,
                        currency=(
                            GymConfigService.get_gym_config(db, tenant_id).payment_pricing.currency
                            or "QAR"
                        ).upper(),
                        balance_before=bal_before,
                        balance_after=bal_after,
                        created_by=user.user_type or "member",
                        created_by_id=user.id,
                    )
                    db.add(txn)
                    db.flush()
                    wallet_txn_id = txn.id
                    user.wallet = bal_after

            booking = ClassBooking(
                tenant_id=tenant_id,
                user_id=user.id,
                class_id=class_id,
                seat_id=_normalize_seat_label(seat_id),
                status=status_str,
                waiting_position=outcome.waiting_position if status_str == WAITING_STATUS else None,
                booked_at=now,
                confirmed_at=now if status_str == "confirmed" else None,
                payment_mode=payment_mode,
                user_package_purchase_id=sale_id,
                sessions_deducted=sessions_deducted,
                notes=notes,
            )
            db.add(booking)
            db.flush()
            if not booking.order_id:
                booking.order_id = f"ORD{str(booking.id).split('-')[0].upper()}"
            # Keep audit marker in notes (DB no longer stores wallet_txn_id on booking).
            if wallet_txn_id is not None:
                booking.notes = _append_bfy_wtxn_note(booking.notes, wallet_txn_id, "debit")

            has_layout = _class_has_layout(gym_class)
            seat_label = _normalize_seat_label(seat_id)
            if has_layout and seat_label and status_str in OCCUPYING_SLOT_STATUSES:
                _set_layout_seat_status(gym_class, seat_label, "booked")

            if status_str == "confirmed":
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

        db.refresh(booking)
        return booking

    @staticmethod
    def cancel(
        db: Session,
        tenant_id: UUID,
        user: User,
        class_id: UUID,
        booking_id: UUID,
        reason: Optional[str],
    ) -> ClassBooking:
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

        if booking.status in (CANCELLED_STATUS, "completed"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Booking already {booking.status}",
            )

        gym_class = (
            db.query(GymClass)
            .filter(GymClass.id == class_id)
            .first()
        )
        if not gym_class:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found")

        cfg = GymConfigService.get_gym_config(db, tenant_id)
        tz = _tenant_tz(db, tenant_id)
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

        # Return package session on cancellation if one was deducted.
        if (
            booking.payment_mode == "package"
            and booking.user_package_purchase_id is not None
            and int(booking.sessions_deducted or 0) > 0
        ):
            sale = (
                db.query(Sale)
                .filter(
                    Sale.id == booking.user_package_purchase_id,
                    Sale.tenant_id == tenant_id,
                    Sale.user_id == user.id,
                )
                .first()
            )
            if sale:
                _restore_sessions_to_sale(sale, int(booking.sessions_deducted or 0))

        tx_ctx = db.begin_nested() if db.in_transaction() else db.begin()
        with tx_ctx:
            booking.status = CANCELLED_STATUS
            booking.cancelled_at = now
            booking.cancelled_by_user_id = user.id
            booking.cancellation_reason = (reason or "").strip() or None

            if previous_status == "confirmed":
                gym_class.booking_counts = max(0, int(gym_class.booking_counts or 0) - 1)
                BookingsService._promote_next_waiting(db, tenant_id, gym_class, now)
            if seat_label:
                seat_still_taken = (
                    db.query(ClassBooking)
                    .filter(
                        ClassBooking.class_id == class_id,
                        cast(ClassBooking.seat_id, SAString) == seat_label,
                        ClassBooking.id != booking.id,
                        ClassBooking.status.in_(list(ACTIVE_USER_BOOKING_STATUSES)),
                    )
                    .first()
                )
                if not seat_still_taken:
                    _set_layout_seat_status(gym_class, seat_label, "available")

        db.refresh(booking)
        return booking
