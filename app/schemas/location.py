from pydantic import BaseModel
from typing import Optional, List
from uuid import UUID

from app.schemas.transactions import PaginationMeta


class LocationBase(BaseModel):
    name: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None


class LocationResponse(LocationBase):
    id: UUID

    class Config:
        from_attributes = True


class LocationsListResponse(BaseModel):
    success: bool = True
    message: str = "Locations fetched successfully"
    data: List[LocationResponse]
    count: int
    pagination: Optional[PaginationMeta] = None

