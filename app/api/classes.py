from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id, get_current_active_user
from app.schemas.gym_class import (
    GymClassResponse,
    ClassesListResponse,
    ClassDetailsOuterResponse,
)
from app.services.classes_service.classes_service import ClassesService
import uuid
from app.models.fitness_program import FitnessProgram
from app.models.gym_class import GymClass
from app.models.user import User

router = APIRouter()


@router.get("/locations/{location_id}/classes", response_model=ClassesListResponse)
async def get_classes_by_date_for_location(
    location_id: uuid.UUID,
    days: int = Query(
        7,
        ge=1,
        le=30,
        description="How many days ahead from today to include (e.g. 7 or 15).",
    ),
    search: Optional[str] = Query(None, description="Search classes by title"),
    sort_by: Optional[str] = Query(
        None, description="Sort by: date, start_time, title"
    ),
    sort_order: str = Query(
        "asc", description="Sort direction: asc or desc"
    ),
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Location-scoped classes list.
    Route: /api/v1/locations/{location_id}/classes
    """
    start_date = date.today()
    end_date = start_date + timedelta(days=days - 1)

    classes = ClassesService.list_classes(
        db,
        tenant_id=tenant_id,
        start_date=start_date,
        end_date=end_date,
        location_id=location_id,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    data = []
    for c in classes:
        item = GymClassResponse.model_validate(c).model_dump()
        live_layout = ClassesService._with_live_layout_status(db, c)
        item["layouts"] = live_layout
        item["fully_booked"] = ClassesService.fully_booked_for_class(c, live_layout)
        data.append(GymClassResponse.model_validate(item))
    return {
        "success": True,
        "message": "Classes fetched successfully",
        "data": data,
        "count": len(classes),
    }


@router.get(
    "/locations/{location_id}/classes/{class_id}",
    response_model=ClassDetailsOuterResponse,
)
async def get_class_details_for_location(
    location_id: uuid.UUID,
    class_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Location-scoped class details.
    Route: /api/v1/locations/{location_id}/classes/{class_id}

    Note: This currently validates the class is within the requested location.
    """
    # DB-level guard: gym_classes has no direct location_id.
    # We validate through gym_classes.training_programme_id -> fitness_programs.location_id.
    gym_class = db.query(GymClass).filter(GymClass.id == class_id).first()
    if not gym_class:
        return {
            "success": True,
            "message": "Class not found",
            "data": None,
        }

    prog_id = getattr(gym_class, "training_programme_id", None)
    try:
        prog_id_int = int(prog_id) if prog_id is not None else 0
    except (TypeError, ValueError):
        prog_id_int = 0

    if prog_id_int <= 0:
        return {
            "success": True,
            "message": "Class not found",
            "data": None,
        }

    program_ok = (
        db.query(FitnessProgram.id)
        .filter(
            FitnessProgram.id == prog_id_int,
            FitnessProgram.tenant_id == tenant_id,
            FitnessProgram.location_id == location_id,
        )
        .first()
    )
    if not program_ok:
        return {
            "success": True,
            "message": "Class not found",
            "data": None,
        }

    payload = ClassesService.get_class_details(
        db=db,
        tenant_id=tenant_id,
        class_id=class_id,
        user_id=current_user.id,
    )

    if payload is None:
        return {
            "success": True,
            "message": "Class not found",
            "data": None,
        }

    return {
        "success": True,
        "message": "Class details fetched successfully",
        "data": payload,
    }
