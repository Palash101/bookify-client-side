from pydantic import BaseModel, field_serializer
from typing import Optional, List

from app.schemas.transactions import PaginationMeta


class FitnessProgramResponse(BaseModel):
    id: int

    name: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None

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

    @field_serializer("show_spots_left")
    def serialize_show_spots_left(self, value: Optional[bool]) -> Optional[bool]:
        return True if value is True else None

    class Config:
        from_attributes = True


class FitnessProgramsListResponse(BaseModel):
    success: bool = True
    message: str = "Training programs fetched successfully"
    data: List[FitnessProgramResponse]
    count: int
    pagination: Optional[PaginationMeta] = None


class FitnessProgramDetailResponse(BaseModel):
    success: bool = True
    message: str = "Training program detail fetched successfully"
    data: FitnessProgramResponse

