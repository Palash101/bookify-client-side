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
    id: UUID  # sale / order id
    package_id: UUID
    package_name: Optional[str] = None
    package_description: Optional[str] = None
    validity_days: Optional[int] = None
    validity_end: Optional[date] = None

    status: str
    purchased_at: datetime
    expires_at: Optional[datetime] = None

    # Sale / payment — what user actually paid (no full pricing/discount tree)
    sale_type: Optional[str] = None  # package_gateway | package_wallet
    amount: Optional[Decimal] = None
    currency: Optional[str] = None

    # Sessions / classes quota for this purchase (from pricing + sale metadata + usage)
    session_type: Optional[str] = None  # e.g. sessions, class
    is_unlimited: bool = False
    session_count: Optional[int] = None  # total included; null if unlimited or unknown
    sessions_remaining: Optional[int] = None  # null if unlimited; else left on this sale
    # Backward-compat: some clients expect singular key name
    remaining_session: Optional[int] = None
    sessions_used: int = 0  # sum of sessions deducted on active bookings for this sale


class ActivePackageResponse(BaseModel):
    success: bool = True
    message: str = "Active package fetched successfully"
    data: Optional[ActivePackageData] = None
