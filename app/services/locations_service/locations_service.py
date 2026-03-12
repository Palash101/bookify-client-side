from typing import List, Optional
import uuid

from sqlalchemy.orm import Session

from app.models.location import Location


class LocationsService:
    @staticmethod
    def list_locations(
        db: Session,
        tenant_id: uuid.UUID,
        only_active: bool = True,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
    ) -> List[Location]:
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

        return query.all()

