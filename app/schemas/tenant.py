from pydantic import BaseModel, Field
from typing import Optional


class TenantResponse(BaseModel):
    # NOTE: For client `/gym` endpoint this is sourced from master DB `Organization`.
    id: str = Field(validation_alias="organization_id")
    business_name: Optional[str] = Field(default=None, validation_alias="name")
    domain: Optional[str] = None

    class Config:
        from_attributes = True
        populate_by_name = True


class GymTenantResponse(BaseModel):
    success: bool = True
    message: str = "Gym details fetched successfully"
    data: TenantResponse

