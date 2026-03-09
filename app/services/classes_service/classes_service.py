from datetime import date
from typing import Optional, List

from sqlalchemy.orm import Session

from app.models.gym_class import GymClass


class ClassesService:
    @staticmethod
    def list_classes(db: Session, class_date: Optional[date] = None) -> List[GymClass]:
        query = db.query(GymClass)
        if class_date is not None:
            query = query.filter(GymClass.class_date == class_date)
        return query.order_by(GymClass.class_date, GymClass.start_time).all()

