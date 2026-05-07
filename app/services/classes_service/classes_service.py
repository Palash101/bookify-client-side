from copy import deepcopy
from datetime import date, datetime
from typing import Optional, List, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session, aliased
from sqlalchemy import and_, func, or_

from app.models.class_booking import ClassBooking
from app.models.gym_class import GymClass
from app.models.user import User
from app.models.tenant import Tenant
from app.models.fitness_program import FitnessProgram
from app.models.location import Location
from app.services.bookings_service import _effective_capacity

# Map common DB abbreviations to IANA timezone names (zoneinfo does not accept "IST" etc.)
COMMON_TZ_ABBREVS = {
    "IST": "Asia/Kolkata",
    "GST": "Asia/Dubai",
    "QAT": "Asia/Qatar",
    "AST": "Asia/Riyadh",
    "PKT": "Asia/Karachi",
    "UTC": "UTC",
}

ACTIVE_LAYOUT_SEAT_STATUSES = ("confirmed", "pending", "pending_payment", "waiting")


class ClassesService:
    @staticmethod
    def _with_live_layout_status(db: Session, gym_class: GymClass) -> Any:
        """
        Returns class layouts payload with seats status reconciled against active bookings.
        """
        raw = getattr(gym_class, "layouts", None)
        if not isinstance(raw, dict):
            return raw
        seats = raw.get("seats")
        if not isinstance(seats, list):
            return raw

        occupied_rows = (
            db.query(ClassBooking.seat_id)
            .filter(
                ClassBooking.class_id == gym_class.id,
                ClassBooking.status.in_(list(ACTIVE_LAYOUT_SEAT_STATUSES)),
                ClassBooking.seat_id.isnot(None),
            )
            .all()
        )
        occupied = {str(r[0]) for r in occupied_rows if r and r[0] is not None}

        layout = deepcopy(raw)
        out_seats = layout.get("seats")
        if not isinstance(out_seats, list):
            return raw
        for seat in out_seats:
            if not isinstance(seat, dict):
                continue
            sid = seat.get("id")
            if sid is None:
                continue
            seat["status"] = "booked" if str(sid) in occupied else "available"
        return layout

    @staticmethod
    def _regular_slots_full(db: Session, gym_class: GymClass) -> bool:
        """
        Main capacity full based on active occupying bookings. Waitlist not considered.

        IMPORTANT: Do not rely on seat_id being present on bookings; capacity may be
        full even if seat mapping isn't recorded.
        """
        cap = _effective_capacity(gym_class)
        if cap <= 0:
            return False
        occupying_statuses = ("confirmed", "pending", "pending_payment")
        occupying_n = (
            db.query(func.count(ClassBooking.id))
            .filter(
                ClassBooking.class_id == gym_class.id,
                ClassBooking.status.in_(list(occupying_statuses)),
            )
            .scalar()
            or 0
        )
        try:
            booked = int(occupying_n)
        except (TypeError, ValueError):
            booked = 0
        return booked >= cap

    @staticmethod
    def fully_booked_for_class(db: Session, gym_class: GymClass, live_layout: Any) -> bool:
        """
        True only when no one else can book or join the waitlist.

        - Regular capacity full (same rules as _regular_slots_full).
        - If max_waitings > 0: also require active ``waiting`` bookings >= max_waitings.
        - If max_waitings <= 0 (no waitlist slots): true when regular capacity only is full.
        """
        if not ClassesService._regular_slots_full(db, gym_class):
            return False

        max_w = int(gym_class.max_waitings or 0)
        if max_w <= 0:
            return True

        waiting_n = (
            db.query(func.count(ClassBooking.id))
            .filter(
                ClassBooking.class_id == gym_class.id,
                ClassBooking.status == "waiting",
            )
            .scalar()
            or 0
        )
        return int(waiting_n) >= max_w

    @staticmethod
    def list_classes(
        db: Session,
        tenant_id,
        start_date: date,
        end_date: date,
        location_id: Optional[Any] = None,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
    ) -> List[GymClass]:
        """
        List classes for a tenant in a date range, with optional search and sorting.
        Rules:
          - Classes for this tenant: trainer belongs to tenant, OR training programme belongs to tenant.
          - Status/publish gating is based on gym_classes.status + gym_classes.publish_at.
          - Always include status != 'draft'.
          - For status = 'draft', include only when publish_at <= tenant's current time.
        """
        # Resolve tenant timezone (DB may store "IST" etc.; zoneinfo needs IANA e.g. "Asia/Kolkata")
        tenant: Optional[Tenant] = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        tz_name = (tenant.timezone or "UTC").strip() if tenant else "UTC"
        tz_key = tz_name.upper()
        if tz_key in COMMON_TZ_ABBREVS:
            tz_name = COMMON_TZ_ABBREVS[tz_key]
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        tenant_now: datetime = datetime.now(tz)

        fp = aliased(FitnessProgram)
        query = (
            db.query(GymClass)
            .outerjoin(User, GymClass.trainer_id == User.id)
            .outerjoin(
                fp,
                and_(
                    fp.id == GymClass.training_programme_id,
                    fp.tenant_id == tenant_id,
                ),
            )
            .filter(
                GymClass.class_date >= start_date,
                GymClass.class_date <= end_date,
                or_(
                    GymClass.trainer_id.is_(None),
                    User.tenant_id == tenant_id,
                    fp.id.isnot(None),
                ),
            )
        )

        # Optional filter by training programme location_id
        if location_id is not None:
            query = query.filter(fp.location_id == location_id)

        # Search by title
        if search:
            like = f"%{search}%"
            query = query.filter(GymClass.title.ilike(like))

        # Sorting
        sort_column = None
        if sort_by == "date":
            sort_column = GymClass.class_date
        elif sort_by == "start_time":
            sort_column = GymClass.start_time
        elif sort_by == "title":
            sort_column = GymClass.title

        if sort_column is not None:
            query = query.order_by(
                sort_column.asc() if sort_order.lower() == "asc" else sort_column.desc()
            )
        else:
            # Default ordering
            query = query.order_by(GymClass.class_date, GymClass.start_time)

        all_classes: List[GymClass] = query.all()

        # Post-filter draft classes by publish_at vs tenant current time
        result: List[GymClass] = []
        for gym_class in all_classes:
            status = (gym_class.status or "").lower()
            if status == "draft":
                publish_at = gym_class.publish_at
                if publish_at is None:
                    continue
                if publish_at <= tenant_now:
                    result.append(gym_class)
                continue

            result.append(gym_class)

        return result

    @staticmethod
    def get_class_details(
        db: Session,
        tenant_id,
        class_id,
        user_id,
    ):
        """
        Returns a single class details payload.

        Note: Seat layout/bookings tables are not present in current models, so
        layout seats are synthesized from max_bookings/booking_counts and
        user_booking is returned as empty.
        """
        gym_class = (
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

        if not gym_class:
            return None

        trainer = None
        if gym_class.trainer_id:
            trainer = db.query(User).filter(User.id == gym_class.trainer_id).first()

        program = None
        if gym_class.training_programme_id and int(gym_class.training_programme_id) != 0:
            program = (
                db.query(FitnessProgram)
                .filter(
                    FitnessProgram.id == int(gym_class.training_programme_id),
                    FitnessProgram.tenant_id == tenant_id,
                )
                .first()
            )

        location = None
        if program and program.location_id:
            location = (
                db.query(Location)
                .filter(Location.id == program.location_id, Location.tenant_id == tenant_id)
                .first()
            )

        # Capacity for UI should match booking logic:
        # - total = layouts.totalSeats (if present) else max_bookings (<=0 means unlimited)
        # - booked = active occupying bookings (confirmed/pending/pending_payment)
        total = int(_effective_capacity(gym_class) or 0)
        occupying_statuses = ("confirmed", "pending", "pending_payment")
        occupying_raw = (
            db.query(func.count(ClassBooking.id))
            .filter(
                ClassBooking.tenant_id == tenant_id,
                ClassBooking.class_id == class_id,
                ClassBooking.status.in_(list(occupying_statuses)),
            )
            .scalar()
            or 0
        )
        try:
            booked = int(occupying_raw)
        except (TypeError, ValueError):
            booked = 0

        max_waitings = int(gym_class.max_waitings or 0)
        available = max(0, total - booked) if total > 0 else 0

        current_waiting_raw = (
            db.query(func.count(ClassBooking.id))
            .filter(
                ClassBooking.tenant_id == tenant_id,
                ClassBooking.class_id == class_id,
                ClassBooking.status == "waiting",
            )
            .scalar()
            or 0
        )
        try:
            current_waiting = int(current_waiting_raw)
        except (TypeError, ValueError):
            current_waiting = 0
        waiting_available = (
            max(0, max_waitings - current_waiting) if max_waitings > 0 else 0
        )

        columns = 5
        rows = 0
        if total > 0:
            rows = (total + columns - 1) // columns

        # Synthesize seat grid: first `booked` seats are booked, rest available.
        seats = []
        row_letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for i in range(rows):
            row_letter = row_letters[i] if i < len(row_letters) else f"R{i+1}"
            for col in range(1, columns + 1):
                seat_index = i * columns + (col - 1)
                if seat_index >= total:
                    break
                status = "booked" if seat_index < booked else "available"
                seats.append(
                    {
                        "id": f"{row_letter}{col}",
                        "row": row_letter,
                        "col": col,
                        "status": status,
                        "type": "mat",
                        "booking_id": None,
                    }
                )
        # If there are no layout seats configured, return empty.

        # Current user's booking (if any) for this class.
        active_statuses = ("confirmed", "waiting", "pending", "pending_payment")
        booking = (
            db.query(ClassBooking)
            .filter(
                ClassBooking.tenant_id == tenant_id,
                ClassBooking.class_id == class_id,
                ClassBooking.user_id == user_id,
                ClassBooking.status.in_(list(active_statuses)),
            )
            .order_by(ClassBooking.created_at.desc())
            .first()
        )

        live_layout = ClassesService._with_live_layout_status(db, gym_class)

        # Prepare response payload expected by schema
        payload = {
            "class_id": str(gym_class.id),
            "name": gym_class.title or gym_class.theme_name or None,
            "gender": gym_class.gender,
            "booking_type": gym_class.booking_type,
            "layout_id": gym_class.layout_id,
            "layouts": live_layout,
            "fully_booked": ClassesService.fully_booked_for_class(db, gym_class, live_layout),
            "program": {
                "id": int(program.id) if program else 0,
                "name": program.name if program else None,
            },
            "trainer": {
                "id": str(trainer.id) if trainer else "",
                "name": f"{trainer.first_name or ''} {trainer.last_name or ''}".strip() if trainer else None,
                "avatar": trainer.avatar if trainer else None,
            },
            "location": {
                "id": str(location.id) if location else "",
                "name": location.name if location else None,
            },
            "schedule": {
                "date": gym_class.class_date,
                "start_time": gym_class.start_time,
                "end_time": gym_class.end_time,
            },
            "capacity": {
                "total": total,
                "booked": booked,
                "available": available,
                "max_waiting": max_waitings,
                "current_waiting": current_waiting,
                "waiting_available": waiting_available,
            },
            "pricing": {
                "drop_in_price": float(gym_class.price) if gym_class.price is not None else None,
                "wallet_credits_required": None,
                "currency": "QAR",
            },
            "user_booking": {
                "has_booked": booking is not None,
                "booking_id": str(booking.id) if booking is not None else None,
                "seat_id": booking.seat_id if booking is not None else None,
                "status": booking.status if booking is not None else None,
                "waiting_position": booking.waiting_position if booking is not None else None,
                "payment_mode": booking.payment_mode if booking is not None else None,
                "package_id": (
                    str(booking.user_package_purchase_id)
                    if booking is not None and booking.user_package_purchase_id is not None
                    else None
                ),
            },
        }
        return payload

