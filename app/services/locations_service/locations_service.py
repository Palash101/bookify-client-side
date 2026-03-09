from typing import List
import uuid

from sqlalchemy.orm import Session

from app.models.location import Location


class LocationsService:
    @staticmethod
    def list_locations(
        db: Session,
        tenant_id: uuid.UUID,
        only_active: bool = True,
    ) -> List[Location]:
        query = db.query(Location).filter(Location.tenant_id == tenant_id)
        if only_active:
            query = query.filter(Location.is_active.is_(True))
        return query.order_by(Location.name).all()

