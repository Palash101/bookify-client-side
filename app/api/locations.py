from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id
from app.schemas.location import LocationResponse, LocationsListResponse
from app.services.locations_service.locations_service import LocationsService
import uuid


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
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Get all active locations for the current tenant with optional search and sorting.
    Requires X-Tenant-Key header.
    """
    locations = LocationsService.list_locations(
        db,
        tenant_id=tenant_id,
        only_active=True,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return {
        "success": True,
        "message": "Locations fetched successfully",
        "data": [LocationResponse.model_validate(l) for l in locations],
        "count": len(locations),
    }

