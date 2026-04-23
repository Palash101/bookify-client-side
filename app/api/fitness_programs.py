from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id
from app.schemas.fitness_program import (
    FitnessProgramResponse,
    FitnessProgramsListResponse,
)
from app.services.fitness_programs_service.fitness_programs_service import FitnessProgramsService
import uuid


router = APIRouter()


@router.get(
    "/locations/{location_id}/training-programs",
    response_model=FitnessProgramsListResponse,
)
async def get_training_programs_for_location(
    location_id: uuid.UUID,
    search: Optional[str] = Query(None, description="Search programs by name"),
    sort_by: Optional[str] = Query(
        None, description="Sort by: name, created_at, display_position"
    ),
    sort_order: str = Query(
        "asc", description="Sort direction: asc or desc"
    ),
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Location-scoped training programs.
    Route: /api/v1/locations/{location_id}/training-programs
    """
    programs = FitnessProgramsService.list_programs(
        db,
        tenant_id=tenant_id,
        location_id=location_id,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        only_active=True,
    )

    return {
        "success": True,
        "message": "Training programs fetched successfully",
        "data": [FitnessProgramResponse.model_validate(p) for p in programs],
        "count": len(programs),
    }

