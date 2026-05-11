from typing import List, Optional, Sequence
import uuid

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.role import Role


class TrainersService:
    @staticmethod
    def list_trainers_by_role_keys(
        db: Session,
        tenant_id: uuid.UUID,
        role_keys: Sequence[str],
        only_active: bool = True,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
    ) -> List[User]:
        """
        List trainers (users) whose roles.key is one of role_keys, with optional search/sort.
        """
        keys = tuple(k for k in role_keys if k)
        if not keys:
            return []

        query = (
            db.query(User)
            .join(Role, User.role_id == Role.id)
            .filter(Role.key.in_(keys), User.tenant_id == tenant_id)
        )
        if only_active:
            # DB may contain NULL for legacy rows; treat NULL as active.
            query = query.filter(or_(User.is_active.is_(True), User.is_active.is_(None)))

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
        """Backward-compatible: single role key."""
        return TrainersService.list_trainers_by_role_keys(
            db,
            tenant_id=tenant_id,
            role_keys=(role_key,),
            only_active=only_active,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order,
        )

