from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id, get_current_active_user
from app.schemas.package import (
    PackageResponse,
    AllPackagesListResponse,
    PackageDetailResponse,
    ActivePackageResponse,
    ActivePackageData,
)
from app.services.packages_service.packages_service import PackagesService
from app.models.user import User
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


@router.get("/active", response_model=ActivePackageResponse)
async def get_active_package(
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Get current user's latest successful package for this tenant.
    Returns null data if user has no active package.
    """
    package = PackagesService.get_active_package_for_user(
        db, tenant_id=tenant_id, user_id=current_user.id
    )

    if not package or not hasattr(package, "_active_order_id"):
        return {
            "success": True,
            "message": "No active package found",
            "data": None,
        }

    return {
        "success": True,
        "message": "Active package fetched successfully",
        "data": ActivePackageData(
            id=package._active_order_id,
            package_id=package.id,
            package_name=package.name,
            package_description=package.description,
            validity_days=package.validity_days,
            validity_end=package.validity_end,
            status=package._active_order_status,
            purchased_at=package._active_order_created_at,
            expires_at=getattr(package, "_active_order_expires_at", None),
            sale_type=getattr(package, "_active_sale_type", None),
            amount=getattr(package, "_active_order_amount", None),
            currency=getattr(package, "_active_order_currency", None),
            session_type=getattr(package, "_active_session_type", None),
            is_unlimited=getattr(package, "_active_is_unlimited", False),
            session_count=getattr(package, "_active_session_count", None),
            sessions_remaining=getattr(package, "_active_sessions_remaining", None),
            remaining_session=getattr(package, "_active_sessions_remaining", None),
            sessions_used=getattr(package, "_active_sessions_used", 0),
        ),
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
