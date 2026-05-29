from pydantic import BaseModel
from typing import Optional, List, Any
from uuid import UUID


class TrainerResponse(BaseModel):
    """User fields for trainer role - no password or sensitive data."""
    id: UUID
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    avatar: Optional[str] = None
    designation: Optional[str] = None
    # JSONB: may be {}, [] or null in DB
    skills: Optional[Any] = None

    class Config:
        from_attributes = True


class TrainersListResponse(BaseModel):
    success: bool = True
    message: str = "Trainers fetched successfully"
    data: List[TrainerResponse]
    count: int
