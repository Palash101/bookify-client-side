from typing import List, Optional
import uuid

from sqlalchemy.orm import Session

from app.models.fitness_program import FitnessProgram


class FitnessProgramsService:
    @staticmethod
    def list_programs(
        db: Session,
        tenant_id: uuid.UUID,
        location_id: Optional[uuid.UUID] = None,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
        only_active: bool = True,
    ) -> List[FitnessProgram]:
        """
        List training programs for a tenant with optional filters.
        """
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

        return query.all()


