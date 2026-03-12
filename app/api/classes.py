from datetime import date
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
    date_filter: Optional[date] = Query(
        None,
        alias="date",
        description="Optional. Date in YYYY-MM-DD to filter classes. Omit to get all classes.",
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
    Get gym classes with optional date filter, search and sorting.
    Date filter param name is `date`.
    Requires X-Tenant-Key header.
    """
    classes = ClassesService.list_classes(
        db,
        class_date=date_filter,
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
