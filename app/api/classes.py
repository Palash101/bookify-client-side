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
    class_date: Optional[date] = Query(None, description="Optional. Date in YYYY-MM-DD to filter classes. Omit to get all classes."),
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Get gym classes. Pass class_date to filter by date; omit to get all classes.
    Requires X-Tenant-Key header.
    """
    classes = ClassesService.list_classes(db, class_date=class_date)
    return {
        "success": True,
        "message": "Classes fetched successfully",
        "data": [GymClassResponse.model_validate(c) for c in classes],
        "count": len(classes),
    }
