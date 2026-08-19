from typing import List, Optional
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.location import Location


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
    def list_locations(
        db: Session,
        tenant_id: str,
        only_active: bool = True,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
        page: int = 1,
        limit: int = 20,
    ) -> tuple[List[Location], int]:
        """
        List locations for a tenant with optional search and sorting.
        """
        query = db.query(Location).filter(Location.tenant_id == tenant_id)
        if only_active:
            query = query.filter(Location.is_active.is_(True))

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
        return query.offset(offset).limit(limit).all(), total

