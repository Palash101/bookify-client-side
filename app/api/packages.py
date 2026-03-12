from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id
from app.schemas.package import PackageResponse, AllPackagesListResponse, PackageDetailResponse
from app.services.packages_service.packages_service import PackagesService
import uuid

router = APIRouter()


@router.get("", response_model=AllPackagesListResponse)
async def get_all_packages(
    search: Optional[str] = Query(None, description="Search packages by name"),
    sort_by: Optional[str] = Query(
        None, description="Sort by: name, created_at, validity_days, sort_order"
    ),
    sort_order: str = Query(
        "asc", description="Sort direction: asc or desc"
    ),
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Get all packages for the current tenant with optional search and sorting.
    Requires X-Tenant-Key header.
    """
    packages = PackagesService.list_packages(
        db,
        tenant_id=tenant_id,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return {
        "success": True,
        "message": "Packages fetched successfully",
        "data": [PackageResponse.model_validate(p) for p in packages],
        "count": len(packages),
    }


@router.get("/{package_id}", response_model=PackageDetailResponse)
async def get_package_detail(
    package_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Get single package detail by ID. Package must belong to current tenant.
    Requires X-Tenant-Key header.
    """
    package = PackagesService.get_package_detail(db, tenant_id=tenant_id, package_id=package_id)
    return {
        "success": True,
        "message": "Package detail fetched successfully",
        "data": PackageResponse.model_validate(package),
    }
