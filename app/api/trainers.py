from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id
from app.schemas.trainer import TrainerResponse, TrainersListResponse
from app.services.trainers_service.trainers_service import TrainersService
import uuid

router = APIRouter()

SENIOR_TRAINER_ROLE_KEY = "senior_trainer"


@router.get("/trainers-get", response_model=TrainersListResponse)
async def get_trainers(
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Get all users whose role key is senior_trainer (from users + roles).
    Requires X-Tenant-Key header.
    """
    trainers = TrainersService.list_trainers_by_role_key(
        db,
        tenant_id=tenant_id,
        role_key=SENIOR_TRAINER_ROLE_KEY,
        only_active=True,
    )
    return {
        "success": True,
        "message": "Trainers fetched successfully",
        "data": [TrainerResponse.model_validate(u) for u in trainers],
        "count": len(trainers),
    }
