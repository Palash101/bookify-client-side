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
from app.models.user import User

router = APIRouter()


@router.get("", response_model=ClassesListResponse)
async def get_classes_by_date(
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
    Get gym classes for current tenant from today up to next N days.
    Requires X-Tenant-Key header.
    """
    start_date = date.today()
    end_date = start_date + timedelta(days=days - 1)

    classes = ClassesService.list_classes(
        db,
        tenant_id=tenant_id,
        start_date=start_date,
        end_date=end_date,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    data = []
    for c in classes:
        item = GymClassResponse.model_validate(c).model_dump()
        item["layouts"] = ClassesService._with_live_layout_status(db, c)
        data.append(GymClassResponse.model_validate(item))
    return {
        "success": True,
        "message": "Classes fetched successfully",
        "data": data,
        "count": len(classes),
    }


@router.get("/{class_id}", response_model=ClassDetailsOuterResponse)
async def get_class_details(
    class_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
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

    # payload already matches schema fields expected by ClassDetailsResponse
    return {
        "success": True,
        "message": "Class details fetched successfully",
        "data": payload,
    }
