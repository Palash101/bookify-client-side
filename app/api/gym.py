from fastapi import APIRouter, Depends

from app.dependencies import get_current_tenant
from app.models.master_org import Organization
from app.schemas.tenant import GymTenantResponse, TenantResponse


router = APIRouter()


@router.get("", response_model=GymTenantResponse)
async def get_gym_details(
    tenant: Organization = Depends(get_current_tenant),
):
    """
    Get current gym details for the request domain / X-Tenant-Key.

    TenantMiddleware has already resolved this organization -- from Redis on
    the domain path -- so there is nothing left to look up here.
    """
    return {
        "success": True,
        "message": "Gym details fetched successfully",
        "data": TenantResponse.model_validate(tenant),
    }

