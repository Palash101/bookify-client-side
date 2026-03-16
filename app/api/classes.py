from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.dependencies import get_current_tenant_id
from app.schemas.gym_class import GymClassResponse, ClassesListResponse
from app.services.classes_service.classes_service import ClassesService
import uuid

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
    return {
        "success": True,
        "message": "Classes fetched successfully",
        "data": [GymClassResponse.model_validate(c) for c in classes],
        "count": len(classes),
    }
