from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_active_user, get_current_tenant_id, get_gym_config_for_active_user
from app.models.user import User
from app.schemas.gym_config_value import GymConfigValue
from app.services import trainer_assignment_service
from app.schemas.trainer import TrainerResponse, TrainersListResponse
from app.schemas.transactions import build_pagination
from app.services.trainers_service.trainers_service import TrainersService

router = APIRouter()

# Users must match roles.key (admin-only staff should also appear as trainers).
TRAINER_ROLE_KEYS = ("trainer", "admin")


@router.get("", response_model=TrainersListResponse)
def get_trainers(
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


@router.get("/mine")
def get_my_trainers(
    current_user: User = Depends(get_current_active_user),
    gym_config: GymConfigValue = Depends(get_gym_config_for_active_user),
    db: Session = Depends(get_db),
):
    """The member's assigned trainers, the gym's trainer assignment mode
    (open / assigned_only / mixed) and whether they may book from the app."""
    return {
        "success": True,
        "message": "Your trainers fetched successfully",
        "data": {
            "trainer_assignment_mode": trainer_assignment_service.mode_of(gym_config),
            "can_book_online": current_user.can_book_online is not False,
            "trainers": trainer_assignment_service.my_trainers(db, current_user.tenant_id, current_user),
        },
    }
