from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from uuid import UUID


class LocationBase(BaseModel):
    name: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_active: Optional[bool] = None


class LocationResponse(LocationBase):
    id: UUID
    tenant_id: UUID
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class LocationsListResponse(BaseModel):
    success: bool = True
    message: str = "Locations fetched successfully"
    data: List[LocationResponse]
    count: int

