from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as time_type, timezone as dt_timezone
from decimal import Decimal
from typing import Any, Optional, Sequence, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, attributes

from app.models.class_booking import ClassBooking
from app.models.fitness_program import FitnessProgram
from app.models.gym_class import GymClass
from app.models.sales import Sale
from app.models.tenant import Tenant
from app.models.user import User
from app.models.wallet_transactions import WalletTransaction
from fastapi import HTTPException, status

from app.core.settings import settings
from app.schemas.booking import PaymentMethod
from app.schemas.gym_config_value import GymConfigValue
from app.services.gym_config_service import GymConfigService

logger = logging.getLogger(__name__)

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

# Confirmed + pending (holds a regular slot, not waitlist)
OCCUPYING_SLOT_STATUSES: Tuple[str, ...] = ("confirmed", "pending")

WAITING_STATUS = "waiting"


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
    lid = gym_class.layout_id
    if lid is None:
        return False
    try:
        return int(lid) != 0
    except (TypeError, ValueError):
        return True


def _sessions_remaining_from_sale(sale: Sale) -> Optional[int]:
    meta = sale.extra_metadata or {}
    if not isinstance(meta, dict):
        return None
    for key in ("sessions_remaining", "remaining_sessions", "sessions_left"):
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


def _finalize_booking_validation(outcome: "BookingValidationOutcome", payment_method: str) -> None:
    if outcome.ok:
        ps = outcome.proposed_status or ""
        if payment_method == "gateway" and ps in ("confirmed", "pending"):
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
    if payment_method == "package":
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
        payment_method: PaymentMethod,
        user_package_purchase_id: Optional[UUID],
        seat_id: Optional[str],
        cfg: Optional[GymConfigValue] = None,
    ) -> BookingValidationOutcome:
        outcome = BookingValidationOutcome()
        pm = payment_method

        if settings.DEBUG:
            logger.info(
                "booking.validate start class_id=%s booking_tenant_id=%s user_id=%s payment=%s",
                class_id,
                tenant_id,
                user.id,
                payment_method,
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
        if starts_at and starts_at <= now:
            outcome.set_check("class_not_started", False, message="Class has already started")
            _finalize_booking_validation(outcome, pm)
            return outcome
        outcome.set_check("class_not_started", True)

        # Class billing mode: package-type → package only; price > 0 → wallet/gateway; else → free only.
        pkg_only = _class_is_package_only(gym_class.booking_type)
        class_price = _class_price_decimal(gym_class)
        is_paid = class_price > 0
        if pkg_only:
            allowed_pm: frozenset[str] = frozenset({"package"})
        elif is_paid:
            allowed_pm = frozenset({"wallet", "gateway"})
        else:
            allowed_pm = frozenset({"free"})

        if pm not in allowed_pm:
            if pkg_only:
                pay_msg = "This class is package-only — use payment_method package with a valid package sale."
            elif is_paid:
                pay_msg = (
                    "This class has a price — book with wallet or gateway, not free or package."
                )
            else:
                pay_msg = "This class is free — use payment_method free."
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
        max_bookings = int(gym_class.max_bookings or 0)
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
                outcome.set_check(
                    "seat_selection",
                    False,
                    message='This class has a layout — send seat_id as the seat label (e.g. "A1").',
                )
            else:
                taken = (
                    db.query(ClassBooking)
                    .filter(
                        ClassBooking.class_id == class_id,
                        ClassBooking.seat_id == seat_label,
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
                    message="Seat not required (class has no layout_id)",
                )

        if outcome.ok:
            if has_slot:
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
        payment_method: PaymentMethod,
        user_package_purchase_id: Optional[UUID],
        seat_id: Optional[str],
        notes: Optional[str],
    ) -> ClassBooking:
        outcome = BookingsService.validate(
            db,
            tenant_id,
            user,
            class_id,
            payment_method,
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

        gym_class = outcome.gym_class
        status_str = outcome.proposed_status
        if payment_method == "gateway" and status_str in ("confirmed", "pending"):
            status_str = "pending_payment"

        now = datetime.now(_tenant_tz(db, tenant_id))
        sessions_deducted = 0
        credits_deducted: Optional[Decimal] = None
        wallet_txn_id: Optional[UUID] = None
        package_id: Optional[UUID] = None
        sale_id: Optional[UUID] = None

        if payment_method == "package" and outcome.sale:
            sale = outcome.sale
            sale_id = sale.id
            package_id = sale.package_id
            if status_str in ("confirmed", "waiting"):
                sessions_deducted = 1
                rem = _sessions_remaining_from_sale(sale)
                if rem is not None:
                    meta: dict[str, Any] = dict(sale.extra_metadata or {})
                    meta["sessions_remaining"] = max(0, rem - 1)
                    sale.extra_metadata = meta
                    attributes.flag_modified(sale, "extra_metadata")

        if payment_method == "wallet":
            price = Decimal(str(gym_class.price or 0))
            # Only charge when slot is confirmed; not waitlist / pending approval / gateway hold
            if price > 0 and status_str == "confirmed":
                bal_before = Decimal(str(user.wallet or 0))
                if bal_before < price:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Insufficient wallet balance",
                    )
                bal_after = bal_before - price
                txn = WalletTransaction(
                    user_id=user.id,
                    order_id=None,
                    direction="debit",
                    transaction_type="class_booking",
                    transaction_id=None,
                    status="succeeded",
                    metadata_={"class_id": str(class_id), "tenant_id": str(tenant_id)},
                    amount=price,
                    currency=(GymConfigService.get_gym_config(db, tenant_id).payment_pricing.currency or "QAR").upper(),
                    balance_before=bal_before,
                    balance_after=bal_after,
                    created_by=user.user_type or "member",
                    created_by_id=user.id,
                )
                db.add(txn)
                db.flush()
                wallet_txn_id = txn.id
                user.wallet = bal_after
                credits_deducted = price

        booking = ClassBooking(
            tenant_id=tenant_id,
            user_id=user.id,
            class_id=class_id,
            booking_type=_normalize_booking_type(gym_class.booking_type) or "unknown",
            seat_id=_normalize_seat_label(seat_id),
            trainer_id=gym_class.trainer_id,
            status=status_str,
            waiting_position=outcome.waiting_position if status_str == WAITING_STATUS else None,
            booked_at=now,
            confirmed_at=now if status_str == "confirmed" else None,
            payment_method=payment_method,
            package_id=package_id,
            user_package_purchase_id=sale_id,
            credits_deducted=credits_deducted,
            wallet_txn_id=wallet_txn_id,
            sessions_deducted=sessions_deducted,
            notes=notes,
        )
        db.add(booking)

        if status_str in ("confirmed", "pending") and payment_method != "gateway":
            gym_class.booking_counts = int(gym_class.booking_counts or 0) + 1

        db.commit()
        db.refresh(booking)
        return booking
