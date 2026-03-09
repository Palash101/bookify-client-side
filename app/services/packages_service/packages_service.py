from typing import List
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.models.package import Package


class PackagesService:
    @staticmethod
    def list_packages(db: Session, tenant_id: uuid.UUID) -> List[Package]:
        return (
            db.query(Package)
            .options(joinedload(Package.pricing_list))
            .filter(Package.tenant_id == tenant_id)
            .order_by(Package.sort_order, Package.created_at)
            .all()
        )

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

