from typing import Any, List, Optional
import uuid

from sqlalchemy.orm import Session

from app.core.redis.cache import cache
from app.models.fitness_program import FitnessProgram
from app.schemas.fitness_program import FitnessProgramResponse

# Programs change rarely, but must not go stale for long.
CACHE_TTL = 300


class FitnessProgramsService:
    @staticmethod
    def normalize_show_spots_left(value: Optional[bool]) -> Optional[bool]:
        """Only expose true when explicitly enabled; otherwise null (not false)."""
        return True if value is True else None

    @staticmethod
    def program_short_payload(program: Optional[FitnessProgram]) -> dict[str, Any]:
        if not program:
            return {
                "id": 0,
                "name": None,
                "show_spots_left": None,
                "spot_name": None,
                "spots_left_label": None,
                "training_mode": None,
            }
        return {
            "id": int(program.id),
            "name": program.name,
            "show_spots_left": FitnessProgramsService.normalize_show_spots_left(
                program.show_spots_left
            ),
            "spot_name": program.spot_name,
            "spots_left_label": program.spots_left_label,
            "training_mode": program.training_mode,
        }

    @staticmethod
    def cache_key(tenant_id: str, location_id: Optional[uuid.UUID] = None) -> str:
        """One key per tenant and location, holding its active program list."""
        return f"program:{tenant_id}:{location_id or 'all'}:active"

    @staticmethod
    def invalidate(tenant_id: str, location_id: Optional[uuid.UUID] = None) -> int:
        """Drop the cached list after a program write."""
        return cache.delete(FitnessProgramsService.cache_key(tenant_id, location_id))

    @staticmethod
    def get_active_programs(
        db: Session,
        tenant_id: str,
        location_id: Optional[uuid.UUID] = None,
    ) -> List[FitnessProgramResponse]:
        """
        Every active program for a tenant (optionally one location), in display
        order, served from Redis.

        The list is small and rarely changes, so it is cached whole and reused
        wherever programs are needed. Concurrent misses each run the query --
        it is a cheap indexed lookup, and a fill lock would block the event
        loop for longer than the query itself takes.
        """

        def loader() -> list[dict]:
            query = db.query(FitnessProgram).filter(
                FitnessProgram.tenant_id == tenant_id,
                FitnessProgram.is_active.is_(True),
            )
            if location_id:
                query = query.filter(FitnessProgram.location_id == location_id)
            rows = query.order_by(
                FitnessProgram.display_position, FitnessProgram.created_at
            ).all()
            return [FitnessProgramResponse.model_validate(r).model_dump() for r in rows]

        payload = cache.get_or_set(
            FitnessProgramsService.cache_key(tenant_id, location_id),
            loader,
            ttl=CACHE_TTL,
        )
        return [FitnessProgramResponse.model_validate(row) for row in payload]

    @staticmethod
    def list_programs(
        db: Session,
        tenant_id: str,
        location_id: Optional[uuid.UUID] = None,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
        only_active: bool = True,
        page: int = 1,
        limit: int = 20,
    ) -> tuple[List[FitnessProgramResponse], int]:
        """
        List training programs for a tenant with optional filters.

        A plain request is paginated out of the cached active list. Anything
        searched or sorted goes to the database, so those one-off queries never
        pollute the cache.
        """
        if not search and sort_by is None and only_active:
            rows = FitnessProgramsService.get_active_programs(
                db, tenant_id, location_id
            )
            offset = (page - 1) * limit
            return rows[offset : offset + limit], len(rows)

        query = db.query(FitnessProgram).filter(FitnessProgram.tenant_id == tenant_id)

        if only_active:
            query = query.filter(FitnessProgram.is_active.is_(True))

        if location_id:
            query = query.filter(FitnessProgram.location_id == location_id)

        if search:
            like = f"%{search}%"
            query = query.filter(FitnessProgram.name.ilike(like))

        sort_column = None
        if sort_by == "name":
            sort_column = FitnessProgram.name
        elif sort_by == "created_at":
            sort_column = FitnessProgram.created_at
        elif sort_by == "display_position":
            sort_column = FitnessProgram.display_position

        if sort_column is not None:
            query = query.order_by(
                sort_column.asc() if sort_order.lower() == "asc" else sort_column.desc()
            )
        else:
            # Default order by display_position then created_at
            query = query.order_by(FitnessProgram.display_position, FitnessProgram.created_at)

        total = query.count()
        offset = (page - 1) * limit
        rows = query.offset(offset).limit(limit).all()
        return [FitnessProgramResponse.model_validate(r) for r in rows], total


