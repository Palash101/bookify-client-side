from typing import List, Optional
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.models.package import Package


class PackagesService:
    @staticmethod
    def list_packages(
        db: Session,
        tenant_id: uuid.UUID,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
    ) -> List[Package]:
        """
        List packages for a tenant with optional search and sorting.
        """
        query = (
            db.query(Package)
            .options(joinedload(Package.pricing_list))
            .filter(Package.tenant_id == tenant_id)
        )

        # Simple text search on name
        if search:
            like = f"%{search}%"
            query = query.filter(Package.name.ilike(like))

        # Sorting
        sort_column = None
        if sort_by == "name":
            sort_column = Package.name
        elif sort_by == "created_at":
            sort_column = Package.created_at
        elif sort_by == "validity_days":
            sort_column = Package.validity_days
        elif sort_by == "sort_order":
            sort_column = Package.sort_order

        if sort_column is not None:
            query = query.order_by(
                sort_column.asc() if sort_order.lower() == "asc" else sort_column.desc()
            )
        else:
            # Default ordering
            query = query.order_by(Package.sort_order, Package.created_at)

        return query.all()

    @staticmethod
    def get_package_detail(db: Session, tenant_id: uuid.UUID, package_id: uuid.UUID) -> Package:
        package = (
            db.query(Package)
            .options(joinedload(Package.pricing_list))
            .filter(Package.id == package_id, Package.tenant_id == tenant_id)
            .first()
        )
        if not package:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
        return package

