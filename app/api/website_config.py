from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.dependencies import get_current_tenant_id, get_db
from app.schemas.tenant_website_config import TenantWebsiteConfigResponse
from app.services.tenant_website_config_service import TenantWebsiteConfigService
from app.services.tenant_website_sections_service import TenantWebsiteSectionsService
from app.services.gym_config_service import GymConfigService

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

    sections = TenantWebsiteSectionsService.get_active_default_sections(
        db,
        tenant_id=tenant_id,
        theme_id=row.theme_id,
    )

    gym_config = GymConfigService.get_gym_config(db, tenant_id)

    return {
        "success": True,
        "message": "Website configuration fetched successfully",
        "data": row.model_copy(
            update={
                "currency": gym_config.resolved_currency(),
                "sections": sections,
                "timezone": GymConfigService.get_timezone_name(db, tenant_id),
            }
        ),
    }
