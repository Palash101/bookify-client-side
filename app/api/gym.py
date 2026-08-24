from fastapi import APIRouter, Depends

from app.dependencies import get_current_tenant
from app.models.master_org import Organization
from app.schemas.tenant import GymTenantResponse
from app.services.redis.gym_service import get_or_cache_gym


router = APIRouter()


@router.get("", response_model=GymTenantResponse)
async def get_gym_details(
    tenant: Organization = Depends(get_current_tenant),
):
    """
    Get current gym details for the request domain / X-Tenant-Key.

    Reads Redis first (by organization domain). On a miss, caches the payload
    and returns it.
    """
    return {
        "success": True,
        "message": "Gym details fetched successfully",
        "data": get_or_cache_gym(tenant),
    }

