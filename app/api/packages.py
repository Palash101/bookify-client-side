from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id, get_current_active_user, get_optional_current_user
from app.schemas.package import (
    AllPackagesListResponse,
    PackageDetailResponse,
    ActivePackagesListResponse,
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
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    Get all packages for the current tenant with optional search and sorting.
    Requires X-Tenant-Key header.

    When the caller sends a valid Bearer token, purchased one-time packages are
    omitted from the list and each item includes `already_purchased` / `can_purchase`.
    """
    packages = PackagesService.list_packages(
        db,
        tenant_id=tenant_id,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    user_id = current_user.id if current_user else None
    visible = [
        p
        for p in packages
        if PackagesService.should_show_package_in_catalog(
            db, tenant_id=tenant_id, user_id=user_id, package=p
        )
    ]
    return {
        "success": True,
        "message": "Packages fetched successfully",
        "data": [
            PackagesService.package_to_response(
                db, tenant_id=tenant_id, package=p, user_id=user_id
            )
            for p in visible
        ],
        "count": len(visible),
    }


@router.get("/active", response_model=ActivePackagesListResponse)
async def get_active_packages(
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    All successful, non-expired package purchases for the current user on this tenant.
    Newest first. Use each item's `id` (sale id) as `user_package_purchase_id` when booking.
    """
    entries = PackagesService.get_active_packages_for_user(
        db, tenant_id=tenant_id, user_id=current_user.id
    )

    if not entries:
        return {
            "success": True,
            "message": "No active packages found",
            "data": [],
            "count": 0,
        }

    return {
        "success": True,
        "message": "Active packages fetched successfully",
        "data": [ActivePackageData.model_validate(e) for e in entries],
        "count": len(entries),
    }


@router.get("/{package_id}", response_model=PackageDetailResponse)
async def get_package_detail(
    package_id: uuid.UUID,
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    Get single package detail by ID. Package must belong to current tenant.
    Requires X-Tenant-Key header.

    Purchased one-time packages are not returned when the caller is authenticated.
    """
    package = PackagesService.get_package_detail(db, tenant_id=tenant_id, package_id=package_id)
    user_id = current_user.id if current_user else None
    if not PackagesService.should_show_package_in_catalog(
        db, tenant_id=tenant_id, user_id=user_id, package=package
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
    return {
        "success": True,
        "message": "Package detail fetched successfully",
        "data": PackagesService.package_to_response(
            db, tenant_id=tenant_id, package=package, user_id=user_id
        ),
    }
