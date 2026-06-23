from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel


class TenantWebsiteConfigData(BaseModel):
    id: UUID
    tenant_id: str
    theme_id: Optional[UUID] = None
    theme_name: Optional[str] = None
    about: Optional[str] = None
    logo_url: Optional[str] = None
    footer_logo: Optional[str] = None
    fevicon_icon: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    background_color: Optional[str] = None
    business_hours: Optional[Dict[str, Any]] = None
    support_email: Optional[str] = None
    support_phone: Optional[str] = None
    terms_and_conditions: Optional[str] = None
    privacy_policy: Optional[str] = None
    refund_policy: Optional[str] = None
    terms_last_updated_at: Optional[datetime] = None
    tax_id: Optional[str] = None
    tax_id_type: Optional[str] = None
    invoice_prefix: Optional[str] = None
    invoice_footer_note: Optional[str] = None
    social_links: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TenantWebsiteConfigResponse(BaseModel):
    success: bool = True
    message: str = "Website configuration fetched successfully"
    data: TenantWebsiteConfigData
