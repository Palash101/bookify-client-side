from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.dependencies import get_current_tenant_id, get_db
from app.schemas.tenant_website_config import (
    TenantWebsiteConfigData,
    TenantWebsiteConfigResponse,
)
from app.services.tenant_website_config_service import TenantWebsiteConfigService

router = APIRouter()


@router.get("", response_model=TenantWebsiteConfigResponse)
async def get_website_config(
    tenant_id: str = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Get branding / website configuration for the current organization.
    Requires X-Tenant-Key header (or matching request domain).
    """
    row = TenantWebsiteConfigService.get_active_config(db, tenant_id=tenant_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website configuration not found for this organization",
        )

    return {
        "success": True,
        "message": "Website configuration fetched successfully",
        "data": TenantWebsiteConfigData.model_validate(row),
    }
