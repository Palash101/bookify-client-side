from pydantic import BaseModel
from typing import Optional, Any, List
from datetime import date, datetime
from uuid import UUID
from decimal import Decimal


class PackageDiscountResponse(BaseModel):
    id: UUID
    name: Optional[str] = None
    description: Optional[str] = None
    value: Optional[Decimal] = None
    type: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PackagePricingResponse(BaseModel):
    id: UUID
    package_id: UUID
    price: Optional[Decimal] = None
    discount_id: Optional[UUID] = None
    session_type: Optional[str] = None
    session_count: Optional[int] = None
    is_unlimited: Optional[bool] = None
    persons: Optional[int] = None
    created_at: Optional[datetime] = None
    discount: Optional[PackageDiscountResponse] = None

    class Config:
        from_attributes = True


class PackageResponse(BaseModel):
    id: UUID
    name: Optional[str] = None
    description: Optional[str] = None
    validity_start: Optional[date] = None
    validity_end: Optional[date] = None
    validity_days: Optional[int] = None
    sort_order: Optional[int] = None
    package_features: Optional[Any] = None
    terms_conditions: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    status: Optional[str] = None
    package_type: Optional[str] = None
    tenant_id: Optional[UUID] = None
    booking_restriction: Optional[Any] = None
    pricing_list: List[PackagePricingResponse] = []

    class Config:
        from_attributes = True


class AllPackagesListResponse(BaseModel):
    success: bool = True
    message: str = "Packages fetched successfully"
    data: List[PackageResponse]
    count: int


class PackageDetailResponse(BaseModel):
    success: bool = True
    message: str = "Package detail fetched successfully"
    data: PackageResponse


class ActivePackageData(BaseModel):
    """
    Currently active package info for a user.
    Combines package + basic order metadata.
    """
    order_id: UUID
    package: PackageResponse
    status: str
    purchased_at: datetime


class ActivePackageResponse(BaseModel):
    success: bool = True
    message: str = "Active package fetched successfully"
    data: Optional[ActivePackageData] = None
