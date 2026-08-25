from typing import List, Optional
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.redis.cache import cache
from app.models.location import Location
from app.schemas.location import LocationResponse

# Short TTL: a location list changes rarely, but must not go stale for long.
CACHE_TTL = 300


class LocationsService:
    @staticmethod
    def resolve_location_id(
        db: Session,
        tenant_id: str,
        location_id: Optional[uuid.UUID] = None,
    ) -> Optional[uuid.UUID]:
        """
        Validate an explicit location, or use the tenant's only active location.
        Multi-location tenants without a provided location_id return None.
        """
        if location_id is not None:
            loc = (
                db.query(Location.id)
                .filter(
                    Location.id == location_id,
                    Location.tenant_id == tenant_id,
                    Location.deleted_at.is_(None),
                )
                .first()
            )
            if loc is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid location_id",
                )
            return loc.id

        locations = (
            db.query(Location.id)
            .filter(
                Location.tenant_id == tenant_id,
                Location.is_active.is_(True),
                Location.deleted_at.is_(None),
            )
            .limit(2)
            .all()
        )
        if len(locations) == 1:
            return locations[0].id
        return None

    @staticmethod
    def cache_key(tenant_id: str) -> str:
        """One key per tenant, holding its whole active location list."""
        return f"loc:{tenant_id}:active"

    @staticmethod
    def invalidate(tenant_id: str) -> int:
        """Drop the cached list after a location write."""
        return cache.delete(LocationsService.cache_key(tenant_id))

    @staticmethod
    def get_active_locations(db: Session, tenant_id: str) -> List[LocationResponse]:
        """
        Every active location for a tenant, name-ordered, served from Redis.

        The list is small and rarely changes, so it is cached whole and reused
        wherever locations are needed. Concurrent misses each run the query --
        it is a cheap indexed lookup, and a fill lock would block the event
        loop for longer than the query itself takes.
        """

        def loader() -> list[dict]:
            rows = (
                db.query(Location)
                .filter(Location.tenant_id == tenant_id, Location.is_active.is_(True))
                .order_by(Location.name)
                .all()
            )
            return [LocationResponse.model_validate(r).model_dump() for r in rows]

        payload = cache.get_or_set(
            LocationsService.cache_key(tenant_id), loader, ttl=CACHE_TTL
        )
        return [LocationResponse.model_validate(row) for row in payload]

    @staticmethod
    def list_locations(
        db: Session,
        tenant_id: str,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
        page: int = 1,
        limit: int = 20,
    ) -> tuple[List[LocationResponse], int]:
        """
        List locations for a tenant with optional search and sorting.

        A plain request is paginated out of the cached active list. Anything
        searched or sorted goes to the database, so those one-off queries never
        pollute the cache.
        """
        if not search and sort_by is None:
            rows = LocationsService.get_active_locations(db, tenant_id)
            offset = (page - 1) * limit
            return rows[offset : offset + limit], len(rows)

        query = db.query(Location).filter(
            Location.tenant_id == tenant_id, Location.is_active.is_(True)
        )

        # Search by name
        if search:
            like = f"%{search}%"
            query = query.filter(Location.name.ilike(like))

        # Sorting
        sort_column = None
        if sort_by == "name":
            sort_column = Location.name
        elif sort_by == "created_at":
            sort_column = Location.created_at

        if sort_column is not None:
            query = query.order_by(
                sort_column.asc() if sort_order.lower() == "asc" else sort_column.desc()
            )
        else:
            # Default ordering by name
            query = query.order_by(Location.name)

        total = query.count()
        offset = (page - 1) * limit
        rows = query.offset(offset).limit(limit).all()
        return [LocationResponse.model_validate(r) for r in rows], total
