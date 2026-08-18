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
from app.schemas.transactions import build_pagination, normalize_display_status
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
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """
    Get catalog packages for the current tenant with optional search and sorting.
    Requires X-Tenant-Key header.

    Only published (status=active), non-private packages within the validity window
    (validity_start on or before today, validity_end on or after today when set)
    are returned.

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
    total = len(visible)
    offset = (page - 1) * limit
    page_items = visible[offset : offset + limit]
    return {
        "success": True,
        "message": "Packages fetched successfully",
        "data": [
            PackagesService.package_to_response(
                db, tenant_id=tenant_id, package=p, user_id=user_id
            )
            for p in page_items
        ],
        "count": len(page_items),
        "pagination": build_pagination(page, limit, total),
    }


@router.get("/active", response_model=ActivePackagesListResponse)
async def get_active_packages(
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    """
    All successful, non-expired package purchases for the current user on this tenant.
    Newest first. Use each item's `id` (sale id) as `user_package_purchase_id` when booking.
    """
    entries, total = PackagesService.get_active_packages_for_user(
        db,
        tenant_id=tenant_id,
        user_id=current_user.id,
        page=page,
        limit=limit,
    )

    data = []
    for e in entries:
        item = ActivePackageData.model_validate(e)
        if item.status:
            item = item.model_copy(
                update={"status": normalize_display_status(item.status) or item.status}
            )
        data.append(item)

    if not data and total == 0:
        return {
            "success": True,
            "message": "No active packages found",
            "data": [],
            "count": 0,
            "pagination": build_pagination(page, limit, 0),
        }

    return {
        "success": True,
        "message": "Active packages fetched successfully",
        "data": data,
        "count": len(data),
        "pagination": build_pagination(page, limit, total),
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

    Draft/blocked/private packages, packages outside the validity window, and purchased
    one-time packages (when authenticated) are not returned.
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
