from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id
from app.schemas.trainer import TrainerResponse, TrainersListResponse
from app.services.trainers_service.trainers_service import TrainersService
import uuid

router = APIRouter()

# Users must match roles.key.
TRAINER_ROLE_KEYS = ("trainer",)


@router.get("", response_model=TrainersListResponse)
async def get_trainers(
    search: Optional[str] = Query(None, description="Search trainers by first/last name"),
    sort_by: Optional[str] = Query(
        None, description="Sort by: name, created_at"
    ),
    sort_order: str = Query(
        "asc", description="Sort direction: asc or desc"
    ),
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    List trainer users for this tenant (roles.key = trainer).
    Optional search/sort. Requires valid X-Tenant-Key header.
    """
    trainers = TrainersService.list_trainers_by_role_keys(
        db,
        tenant_id=tenant_id,
        role_keys=TRAINER_ROLE_KEYS,
        only_active=True,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return {
        "success": True,
        "message": "Trainers fetched successfully",
        "data": [TrainerResponse.model_validate(u) for u in trainers],
        "count": len(trainers),
    }
