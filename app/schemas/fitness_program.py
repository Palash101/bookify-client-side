from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from uuid import UUID


class FitnessProgramResponse(BaseModel):
    id: int
    tenant_id: UUID
    location_id: Optional[UUID] = None

    name: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None

    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    is_layout_required: Optional[bool] = None
    spot_name: Optional[str] = None
    show_spots_left: Optional[bool] = None
    spots_left_label: Optional[str] = None
    classes_title_key: Optional[str] = None

    experience_required: Optional[bool] = None
    disallow_first_timers: Optional[bool] = None
    minimum_experience_level: Optional[str] = None

    has_age_restriction: Optional[bool] = None
    min_age: Optional[int] = None
    max_age: Optional[int] = None

    training_mode: Optional[str] = None
    gender_restriction: Optional[str] = None
    display_position: Optional[int] = None

    class Config:
        from_attributes = True


class FitnessProgramsListResponse(BaseModel):
    success: bool = True
    message: str = "Training programs fetched successfully"
    data: List[FitnessProgramResponse]
    count: int


class FitnessProgramDetailResponse(BaseModel):
    success: bool = True
    message: str = "Training program detail fetched successfully"
    data: FitnessProgramResponse

