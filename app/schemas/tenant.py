from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from uuid import UUID


class TenantResponse(BaseModel):
    id: UUID
    business_name: Optional[str] = None
    domain: Optional[str] = None
    status: Optional[str] = None
    timezone: Optional[str] = None
    currency: Optional[str] = None
    terms_accepted: Optional[bool] = None
    type: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class GymTenantResponse(BaseModel):
    success: bool = True
    message: str = "Gym details fetched successfully"
    data: TenantResponse

