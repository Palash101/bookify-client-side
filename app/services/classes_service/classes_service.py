from datetime import date
from typing import Optional, List

from sqlalchemy.orm import Session

from app.models.gym_class import GymClass


class ClassesService:
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
        """
        query = db.query(GymClass).filter(
            GymClass.class_date >= start_date,
            GymClass.class_date <= end_date,
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

        return query.all()

