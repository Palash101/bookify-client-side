from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace
from typing import Optional, List, Any
from uuid import UUID

from sqlalchemy.orm import Session, aliased
from sqlalchemy import and_, func, or_, select

from app.core.redis.cache import cache, tenant_key
from app.models.class_booking import ClassBooking, ClassBookingStatus, class_booking_status_value
from app.models.gym_class import GymClass
from app.models.user import User
from app.models.fitness_program import FitnessProgram
from app.models.location import Location
from app.schemas.gym_class import GymClassResponse
from app.services.bookings_service import _effective_capacity, _tenant_tz, booking_cancel_info
from app.services.fitness_programs_service.fitness_programs_service import FitnessProgramsService
from app.services.gym_config_service import GymConfigService

# Same window as locations: catalog can go stale this long, live booking
# fields are never stored — they are overlaid on every request.
CACHE_TTL = 300

ACTIVE_LAYOUT_SEAT_STATUSES = (
    ClassBookingStatus.confirmed,
    ClassBookingStatus.pending,
    ClassBookingStatus.pending_payment,
    ClassBookingStatus.waiting,
)


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
        occupying_statuses = (
            ClassBookingStatus.confirmed,
            ClassBookingStatus.pending,
            ClassBookingStatus.pending_payment,
        )
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
                ClassBooking.status == ClassBookingStatus.waiting,
            )
            .scalar()
            or 0
        )
        return int(waiting_n) >= max_w

    @staticmethod
    def cache_key(
        tenant_id: str, location_id: Any, start_date: date, end_date: date
    ) -> str:
        """``t:ORG-110:class:{location}:{start}:{end}`` — that window's catalog."""
        return tenant_key(
            tenant_id, "class", location_id, start_date.isoformat(), end_date.isoformat()
        )

    @staticmethod
    def invalidate(tenant_id: str, location_id: Any) -> int:
        """Drop every cached date window for this location after a class write."""
        return cache.delete_prefix(tenant_key(tenant_id, "class", location_id) + ":")

    @staticmethod
    def _fetch_classes(
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
        Classes for a tenant in a date range, with optional search and sorting.
        Rules:
          - Classes for this tenant: trainer belongs to tenant, OR training programme belongs to tenant.
          - Status/publish gating is based on gym_classes.status + gym_classes.publish_at.
          - Always include status != 'draft'.
          - For status = 'draft', include only when publish_at <= tenant's current time.
        """
        gym_config = GymConfigService.get_gym_config(db, tenant_id)
        tz = GymConfigService.resolve_zoneinfo(gym_config)
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

        if location_id is not None:
            query = query.filter(fp.location_id == location_id)

        if search:
            like = f"%{search}%"
            query = query.filter(GymClass.title.ilike(like))

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
            query = query.order_by(GymClass.class_date, GymClass.start_time)

        result: List[GymClass] = []
        for gym_class in query.all():
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
    def list_classes(
        db: Session,
        tenant_id,
        start_date: date,
        end_date: date,
        location_id: Optional[Any] = None,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
        page: int = 1,
        limit: int = 20,
    ) -> tuple[List[GymClass], int]:
        result = ClassesService._fetch_classes(
            db,
            tenant_id,
            start_date,
            end_date,
            location_id=location_id,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        offset = (page - 1) * limit
        return result[offset : offset + limit], len(result)

    @staticmethod
    def _programme_id(raw: Any) -> int:
        try:
            pid = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0
        return pid if pid > 0 else 0

    @staticmethod
    def _catalog_dicts(
        db: Session, tenant_id: str, classes: List[GymClass]
    ) -> list[dict]:
        """Class + trainer + program. Layouts and live occupancy are not cached."""
        trainer_ids = {
            c.trainer_id for c in classes if getattr(c, "trainer_id", None) is not None
        }
        trainer_by_id: dict[str, dict] = {}
        if trainer_ids:
            rows = db.execute(
                select(User.id, User.first_name, User.last_name, User.avatar).where(
                    User.id.in_(list(trainer_ids))
                )
            ).all()
            for tid, first, last, avatar in rows:
                full = f"{first or ''} {last or ''}".strip()
                trainer_by_id[str(tid)] = {"name": full or None, "image": avatar}

        programme_ids = set()
        for c in classes:
            pid = ClassesService._programme_id(getattr(c, "training_programme_id", None))
            if pid:
                programme_ids.add(pid)
        program_by_id: dict[int, FitnessProgram] = {}
        if programme_ids:
            programs = (
                db.query(FitnessProgram)
                .filter(
                    FitnessProgram.tenant_id == tenant_id,
                    FitnessProgram.id.in_(list(programme_ids)),
                )
                .all()
            )
            program_by_id = {int(p.id): p for p in programs}

        payload: list[dict] = []
        for gym_class in classes:
            item = GymClassResponse.model_validate(gym_class).model_dump(mode="json")
            trainer = trainer_by_id.get(str(getattr(gym_class, "trainer_id", "")), {})
            item["trainer_name"] = trainer.get("name")
            item["trainer_image"] = trainer.get("image")
            pid = ClassesService._programme_id(
                getattr(gym_class, "training_programme_id", None)
            )
            item["program"] = FitnessProgramsService.program_short_payload(
                program_by_id.get(pid) if pid else None
            )
            item["fully_booked"] = False
            payload.append(item)
        return payload

    @staticmethod
    def _class_id(row: dict) -> Optional[UUID]:
        class_id = row.get("id")
        if class_id is None:
            return None
        return UUID(class_id) if isinstance(class_id, str) else class_id

    @staticmethod
    def _load_layouts(db: Session, rows: list[dict]) -> dict:
        ids = []
        for row in rows:
            cid = ClassesService._class_id(row)
            if cid is not None:
                ids.append(cid)
        if not ids:
            return {}
        found = (
            db.query(GymClass.id, GymClass.layouts)
            .filter(GymClass.id.in_(ids))
            .all()
        )
        return {class_id: layouts for class_id, layouts in found}

    @staticmethod
    def _live_proxy(row: dict, layouts: Any = None) -> SimpleNamespace:
        return SimpleNamespace(
            id=ClassesService._class_id(row),
            layouts=layouts,
            layout_id=row.get("layout_id"),
            max_bookings=row.get("max_bookings"),
            max_waitings=row.get("max_waitings"),
        )

    @staticmethod
    def _with_live_fields(db: Session, row: dict, layouts: Any = None) -> GymClassResponse:
        item = dict(row)
        item.pop("layouts", None)
        proxy = ClassesService._live_proxy(item, layouts=layouts)
        item["fully_booked"] = ClassesService.fully_booked_for_class(db, proxy, None)
        return GymClassResponse.model_validate(item)

    @staticmethod
    def _with_live_fields_for_page(
        db: Session, rows: list[dict]
    ) -> List[GymClassResponse]:
        layout_by_id = ClassesService._load_layouts(db, rows)
        return [
            ClassesService._with_live_fields(
                db, row, layouts=layout_by_id.get(ClassesService._class_id(row))
            )
            for row in rows
        ]

    @staticmethod
    def get_location_classes(
        db: Session,
        tenant_id: str,
        location_id: Any,
        start_date: date,
        end_date: date,
    ) -> list[dict]:
        """
        Location catalog for a date window, served from Redis.

        Same pattern as locations: the list is cached whole; search/sort never
        writes here. Concurrent misses each run the query.
        """

        def loader() -> list[dict]:
            rows = ClassesService._fetch_classes(
                db,
                tenant_id,
                start_date,
                end_date,
                location_id=location_id,
            )
            return ClassesService._catalog_dicts(db, tenant_id, rows)

        payload = cache.get_or_set(
            ClassesService.cache_key(tenant_id, location_id, start_date, end_date),
            loader,
            ttl=CACHE_TTL,
        )
        return payload if isinstance(payload, list) else []

    @staticmethod
    def list_location_classes(
        db: Session,
        tenant_id: str,
        location_id: Any,
        start_date: date,
        end_date: date,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
    ) -> List[GymClassResponse]:
        """
        Location-scoped class list for the client API.

        A plain request is served from the cached catalog, then live occupancy
        is applied. Search or sort goes to the database so those queries never
        pollute the cache.
        """
        if not search and sort_by is None:
            rows = ClassesService.get_location_classes(
                db, tenant_id, location_id, start_date, end_date
            )
            return ClassesService._with_live_fields_for_page(db, rows)

        rows = ClassesService._fetch_classes(
            db,
            tenant_id,
            start_date,
            end_date,
            location_id=location_id,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        catalog = ClassesService._catalog_dicts(db, tenant_id, rows)
        return ClassesService._with_live_fields_for_page(db, catalog)

    @staticmethod
    def get_class_details(
        db: Session,
        tenant_id,
        class_id,
        user_id: Optional[UUID] = None,
    ):
        """
        Returns a single class details payload.

        When ``user_id`` is set, includes that user's active booking (if any) with
        cancellation eligibility. Without a user, ``user_booking.has_booked`` is false.
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

        gym_config = GymConfigService.get_gym_config(db, tenant_id)

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
        occupying_statuses = (
            ClassBookingStatus.confirmed,
            ClassBookingStatus.pending,
            ClassBookingStatus.pending_payment,
        )
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
                ClassBooking.status == ClassBookingStatus.waiting,
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

        booking = None
        if user_id is not None:
            active_statuses = (
                ClassBookingStatus.confirmed,
                ClassBookingStatus.waiting,
                ClassBookingStatus.pending,
                ClassBookingStatus.pending_payment,
            )
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

        tz = _tenant_tz(db, tenant_id, gym_config=gym_config)
        now = datetime.now(tz)
        can_cancel = False
        cancel_deadline: Optional[str] = None
        if booking is not None:
            can_cancel, cancel_deadline = booking_cancel_info(
                booking, gym_class, gym_config, tz, now
            )

        # Prepare response payload expected by schema
        payload = {
            "class_id": str(gym_class.id),
            "name": gym_class.title or gym_class.theme_name or None,
            "gender": gym_class.gender,
            "booking_type": gym_class.booking_type,
            "layout_id": gym_class.layout_id,
            "layouts": live_layout,
            "fully_booked": ClassesService.fully_booked_for_class(db, gym_class, live_layout),
            "program": FitnessProgramsService.program_short_payload(program),
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
                "currency": gym_config.resolved_currency(),
            },
            "user_booking": {
                "has_booked": booking is not None,
                "booking_id": str(booking.id) if booking is not None else None,
                "seat_id": booking.seat_id if booking is not None else None,
                "status": class_booking_status_value(booking.status) if booking is not None else None,
                "waiting_position": booking.waiting_position if booking is not None else None,
                "payment_mode": booking.payment_mode if booking is not None else None,
                "package_id": (
                    str(booking.user_package_purchase_id)
                    if booking is not None and booking.user_package_purchase_id is not None
                    else None
                ),
                "can_cancel": can_cancel,
                "cancel_deadline": cancel_deadline,
            },
        }
        return payload

