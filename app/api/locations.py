from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id
from app.schemas.location import LocationResponse, LocationsListResponse
from app.schemas.transactions import build_pagination
from app.services.locations_service.locations_service import LocationsService


router = APIRouter()


@router.get("", response_model=LocationsListResponse)
async def get_locations(
    search: Optional[str] = Query(None, description="Search locations by name"),
    sort_by: Optional[str] = Query(
        None, description="Sort by: name, created_at"
    ),
    sort_order: str = Query(
        "asc", description="Sort direction: asc or desc"
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    tenant_id: str = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Get all active locations for the current tenant with optional search and sorting.
    Requires X-Tenant-Key header.
    """
    locations, total = LocationsService.list_locations(
        db,
        tenant_id=tenant_id,
        only_active=True,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        limit=limit,
    )
    return {
        "success": True,
        "message": "Locations fetched successfully",
        "data": [LocationResponse.model_validate(l) for l in locations],
        "count": len(locations),
        "pagination": build_pagination(page, limit, total),
    }
