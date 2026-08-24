from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id
from app.schemas.trainer import TrainerResponse, TrainersListResponse
from app.schemas.transactions import build_pagination
from app.services.trainers_service.trainers_service import TrainersService

router = APIRouter()

# Users must match roles.key (admin-only staff should also appear as trainers).
TRAINER_ROLE_KEYS = ("trainer", "admin")


@router.get("", response_model=TrainersListResponse)
async def get_trainers(
    search: Optional[str] = Query(None, description="Search trainers by first/last name"),
    sort_by: Optional[str] = Query(
        None, description="Sort by: name, created_at"
    ),
    sort_order: str = Query(
        "asc", description="Sort direction: asc or desc"
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    tenant_id: str = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    List trainer users for this tenant (roles.key = trainer or admin).
    Optional search/sort. Requires valid X-Tenant-Key header.
    """
    trainers, total = TrainersService.list_trainers_by_role_keys(
        db,
        tenant_id=tenant_id,
        role_keys=TRAINER_ROLE_KEYS,
        only_active=True,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        limit=limit,
    )
    return {
        "success": True,
        "message": "Trainers fetched successfully",
        "data": [TrainerResponse.model_validate(u) for u in trainers],
        "count": len(trainers),
        "pagination": build_pagination(page, limit, total),
    }
