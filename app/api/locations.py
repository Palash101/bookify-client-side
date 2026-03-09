from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id
from app.schemas.location import LocationResponse, LocationsListResponse
from app.services.locations_service.locations_service import LocationsService
import uuid


router = APIRouter()


@router.get("/get-locations", response_model=LocationsListResponse)
async def get_locations(
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Get all active locations for the current tenant.
    Requires X-Tenant-Key header.
    """
    locations = LocationsService.list_locations(db, tenant_id=tenant_id, only_active=True)
    return {
        "success": True,
        "message": "Locations fetched successfully",
        "data": [LocationResponse.model_validate(l) for l in locations],
        "count": len(locations),
    }

