from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.dependencies import get_current_active_user
from app.models.user import User
from app.schemas.event import MyBadgesListResponse
from app.schemas.transactions import build_pagination
from app.services.events_service.events_service import EventsService

router = APIRouter()


@router.get("/me/badges", response_model=MyBadgesListResponse)
def get_my_badges(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Badges I earned (challenge rewards), newest first."""
    items, total = EventsService.list_my_badges(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        page=page,
        limit=limit,
    )
    return {
        "success": True,
        "message": "Badges fetched successfully" if items else "No badges earned yet",
        "data": items,
        "count": len(items),
        "pagination": build_pagination(page, limit, total),
    }
