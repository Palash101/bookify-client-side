from typing import List, Optional
import uuid

from sqlalchemy import or_
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
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
    ) -> List[User]:
        """
        List trainers (users) by role key with optional search and sorting.
        """
        query = (
            db.query(User)
            .join(Role, User.role_id == Role.id)
            .filter(Role.key == role_key, User.tenant_id == tenant_id)
        )
        if only_active:
            query = query.filter(User.is_active.is_(True))

        # Search by name
        if search:
            like = f"%{search}%"
            query = query.filter(
                or_(User.first_name.ilike(like), User.last_name.ilike(like))
            )

        # Sorting
        sort_column = None
        if sort_by == "name":
            sort_column = User.first_name
        elif sort_by == "created_at":
            sort_column = User.created_at

        if sort_column is not None:
            query = query.order_by(
                sort_column.asc() if sort_order.lower() == "asc" else sort_column.desc()
            )
        else:
            # Default ordering by name
            query = query.order_by(User.first_name, User.last_name)

        return query.all()

