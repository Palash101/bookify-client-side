import json
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.settings import settings
from app.dependencies import get_current_active_user
from app.models.user import User
from app.schemas.booking import (
    BookingCreateResponse,
    BookingCreatedData,
    BookingRequestBody,
    BookingValidateData,
    BookingValidateResponse,
)
from app.services.bookings_service import BookingsService
from app.services.gym_config_service import GymConfigService

router = APIRouter()
_log = logging.getLogger(__name__)


@router.post(
    "/{class_id}/bookings/validate",
    response_model=BookingValidateResponse,
)
async def validate_class_booking(
    request: Request,
    class_id: uuid.UUID,
    body: BookingRequestBody,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Run all booking rules (gym_config, capacity/waitlist, payment path, seat) without writing data.

    Tenant scope = logged-in member's gym (users.tenant_id). X-Tenant-Key is only required by
    middleware as a valid app key (same idea as wallet routes).
    """
    tenant_id = current_user.tenant_id
    cfg = GymConfigService.get_gym_config(db, tenant_id)
    outcome = BookingsService.validate(
        db,
        tenant_id,
        current_user,
        class_id,
        body.payment_method,
        body.user_package_purchase_id,
        body.seat_id,
        cfg=cfg,
    )
    debug = None
    if settings.DEBUG:
        api_key_tid = getattr(request.state, "tenant_id", None)
        debug = BookingsService.debug_validate_context(
            db,
            booking_tenant_id=tenant_id,
            api_key_tenant_id=api_key_tid,
            user=current_user,
            class_id=class_id,
            outcome=outcome,
        )
        _log.info("booking.validate DEBUG %s", json.dumps(debug, default=str))
    return {
        "success": True,
        "message": "Validation complete" if outcome.ok else "Validation failed",
        "data": BookingValidateData(
            valid=outcome.ok,
            checks=outcome.checks_map,
            proceed_to=outcome.proceed_to,
            message=outcome.summary_message,
            proposed_status=outcome.proposed_status,
            waiting_position=outcome.waiting_position,
            debug=debug,
        ),
    }


@router.post(
    "/{class_id}/bookings",
    response_model=BookingCreateResponse,
)
async def create_class_booking(
    class_id: uuid.UUID,
    body: BookingRequestBody,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Validate then create a booking. Re-runs validation on submit (do not trust client-only checks).
    """
    tenant_id = current_user.tenant_id
    booking = BookingsService.create(
        db,
        tenant_id,
        current_user,
        class_id,
        body.payment_method,
        body.user_package_purchase_id,
        body.seat_id,
        body.notes,
    )
    return {
        "success": True,
        "message": "Booking created",
        "data": BookingCreatedData(
            booking_id=booking.id,
            status=booking.status,
            waiting_position=booking.waiting_position,
            payment_method=booking.payment_method,
            sessions_deducted=int(booking.sessions_deducted or 0),
            credits_deducted=booking.credits_deducted,
        ),
    }
