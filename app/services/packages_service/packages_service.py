from typing import List, Optional
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.models.package import Package
from app.models.package_pricing import PackagePricing
from app.models.package_order import PackageOrder


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
            .options(
                joinedload(Package.pricing_list).joinedload(PackagePricing.discount)
            )
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
            .options(
                joinedload(Package.pricing_list).joinedload(PackagePricing.discount)
            )
            .filter(Package.id == package_id, Package.tenant_id == tenant_id)
            .first()
        )
        if not package:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
        return package

    @staticmethod
    def get_active_package_for_user(
        db: Session,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Optional[Package]:
        """
        Return the latest successful & non-expired package for this user+tenant.
        If no active order, returns None.
        """
        from sqlalchemy.sql import func as sa_func

        # Find latest successful, non-expired order
        order = (
            db.query(PackageOrder)
            .filter(
                PackageOrder.tenant_id == tenant_id,
                PackageOrder.user_id == user_id,
                PackageOrder.status == "success",
                # Either no expiry set yet, or still in future
                (PackageOrder.expires_at.is_(None)) | (PackageOrder.expires_at > sa_func.now()),
            )
            .order_by(PackageOrder.created_at.desc())
            .first()
        )

        if not order:
            return None

        package = (
            db.query(Package)
            .options(
                joinedload(Package.pricing_list).joinedload(PackagePricing.discount)
            )
            .filter(Package.id == order.package_id, Package.tenant_id == tenant_id)
            .first()
        )

        # Attach order metadata to package instance for schema mapping if needed
        if package is not None:
            # non-persistent attrs just for response
            package._active_order_id = order.id
            package._active_order_status = order.status
            package._active_order_created_at = order.created_at

        return package

