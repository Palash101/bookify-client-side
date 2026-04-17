from copy import deepcopy
from datetime import date, datetime
from typing import Optional, List, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session
from sqlalchemy import and_, exists, or_

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
    def fully_booked_for_class(gym_class: GymClass, live_layout: Any) -> bool:
        """
        Whether the class should be treated as full for UI (disable booking).

        - If layouts.seats has entries with ids, all such seats must be ``booked``
          (uses live_layout from _with_live_layout_status).
        - Else capacity from ``_effective_capacity`` (totalSeats or max_bookings);
          unlimited (<=0) => not fully_booked.
        """
        if isinstance(live_layout, dict):
            seats = live_layout.get("seats")
            if isinstance(seats, list):
                with_id = [
                    s
                    for s in seats
                    if isinstance(s, dict) and s.get("id") is not None
                ]
                if with_id:
                    for s in with_id:
                        st = str(s.get("status") or "").lower()
                        if st != "booked":
                            return False
                    return True

        cap = _effective_capacity(gym_class)
        if cap <= 0:
            return False
        booked = int(gym_class.booking_counts or 0)
        return booked >= cap

    @staticmethod
    def list_classes(
        db: Session,
        tenant_id,
        start_date: date,
        end_date: date,
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

        programme_for_tenant = exists().where(
            and_(
                FitnessProgram.id == GymClass.training_programme_id,
                FitnessProgram.tenant_id == tenant_id,
            )
        )
        query = (
            db.query(GymClass)
            .outerjoin(User, GymClass.trainer_id == User.id)
            .filter(
                GymClass.class_date >= start_date,
                GymClass.class_date <= end_date,
                or_(
                    GymClass.trainer_id.is_(None),
                    User.tenant_id == tenant_id,
                    programme_for_tenant,
                ),
            )
        )

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

        total = int(gym_class.max_bookings or 0)
        booked = int(gym_class.booking_counts or 0)
        max_waitings = int(gym_class.max_waitings or 0)
        available = max(0, total - booked)
        waiting = max_waitings

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

        # Prepare response payload expected by schema
        payload = {
            "class_id": str(gym_class.id),
            "name": gym_class.title or gym_class.theme_name or None,
            "booking_type": gym_class.booking_type,
            "layout_id": gym_class.layout_id,
            "layouts": ClassesService._with_live_layout_status(db, gym_class),
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
                "waiting": waiting,
                "max_waiting": max_waitings,
                "available": available,
            },
            "pricing": {
                "drop_in_price": float(gym_class.price) if gym_class.price is not None else None,
                "wallet_credits_required": None,
                "currency": "QAR",
            },
            "layout": {
                "rows": rows,
                "columns": columns,
                "seats": seats,
            },
            "user_booking": {
                "has_booked": False,
                "booking_id": None,
                "seat_id": None,
                "status": None,
                "payment_mode": None,
                "package_id": None,
            },
        }
        return payload

