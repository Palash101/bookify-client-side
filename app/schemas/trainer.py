from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from uuid import UUID


class TrainerResponse(BaseModel):
    """User fields for senior_trainer role - no password or sensitive data."""
    id: UUID
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    avatar: Optional[str] = None
    designation: Optional[str] = None
    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TrainersListResponse(BaseModel):
    success: bool = True
    message: str = "Trainers fetched successfully"
    data: List[TrainerResponse]
    count: int
