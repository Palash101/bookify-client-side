from fastapi import APIRouter, Depends

from app.dependencies import get_current_tenant
from app.schemas.tenant import GymTenantResponse, TenantResponse
from app.models.tenant import Tenant


router = APIRouter()


@router.get("", response_model=GymTenantResponse)
async def get_gym_details(
    tenant: Tenant = Depends(get_current_tenant),
):
    """
    Get current gym (tenant) details based on X-Tenant-Key header.
    """
    return {
        "success": True,
        "message": "Gym details fetched successfully",
        "data": TenantResponse.model_validate(tenant),
    }

