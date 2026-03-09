from typing import List
import uuid

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.role import Role


class TrainersService:
    @staticmethod
    def list_trainers_by_role_key(
        db: Session,
        tenant_id: uuid.UUID,
        role_key: str,
        only_active: bool = True,
    ) -> List[User]:
        query = (
            db.query(User)
            .join(Role, User.role_id == Role.id)
            .filter(Role.key == role_key, User.tenant_id == tenant_id)
        )
        if only_active:
            query = query.filter(User.is_active.is_(True))
        return query.order_by(User.first_name, User.last_name).all()

