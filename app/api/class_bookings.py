import json
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.settings import settings
from app.dependencies import get_current_active_user, get_gym_config_for_active_user
from app.models.gym_class import GymClass
from app.models.user import User
from app.schemas.booking import (
    BookingCancelRequestBody,
    BookingCancelResponse,
    BookingCancelledData,
    BookingCreateResponse,
    BookingCreatedData,
    MemberBookingsResponse,
    BookingRequestBody,
    BookingValidateData,
    BookingValidateResponse,
)
from app.schemas.gym_config_value import GymConfigValue
from app.services.bookings_service import BookingsService
from app.services.notification_service import BookingNotificationService
from app.services.wallet_notification_service import WalletNotificationService

router = APIRouter()
_log = logging.getLogger(__name__)


@router.get(
    "/bookings",
    response_model=MemberBookingsResponse,
)
async def get_member_bookings(
    current_user: User = Depends(get_current_active_user),
    gym_config: GymConfigValue = Depends(get_gym_config_for_active_user),
    db: Session = Depends(get_db),
):
    tenant_id = current_user.tenant_id
    return BookingsService.list_member_bookings(
        db, tenant_id, current_user, gym_config=gym_config
    )


@router.post(
    "/{class_id}/bookings/validate",
    response_model=BookingValidateResponse,
)
async def validate_class_booking(
    request: Request,
    class_id: uuid.UUID,
    body: BookingRequestBody,
    current_user: User = Depends(get_current_active_user),
    gym_config: GymConfigValue = Depends(get_gym_config_for_active_user),
    db: Session = Depends(get_db),
):
    """
    Run all booking rules (gym_config, capacity/waitlist, payment path, seat) without writing data.

    Tenant scope = logged-in member's gym (users.tenant_id). X-Tenant-Key is only required by
    middleware as a valid app key (same idea as wallet routes).
    """
    tenant_id = current_user.tenant_id
    outcome = BookingsService.validate(
        db,
        tenant_id,
        current_user,
        class_id,
        body.payment_mode,
        body.user_package_purchase_id,
        body.seat_id,
        cfg=gym_config,
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
    gym_config: GymConfigValue = Depends(get_gym_config_for_active_user),
    db: Session = Depends(get_db),
):
    """
    Validate then create a booking. Re-runs validation on submit (do not trust client-only checks).
    """
    tenant_id = current_user.tenant_id
    booking, wallet_txn_id = BookingsService.create(
        db,
        tenant_id,
        current_user,
        class_id,
        body.payment_mode,
        body.user_package_purchase_id,
        body.seat_id,
        body.notes,
        gym_config=gym_config,
    )
    db.commit()
    if wallet_txn_id is not None:
        await WalletNotificationService.publish_debited(
            db,
            tenant_id=tenant_id,
            wallet_transaction_id=wallet_txn_id,
        )
    status = (booking.status or "").strip().lower()
    if status == "confirmed":
        await BookingNotificationService.publish_confirmed(
            db,
            tenant_id=tenant_id,
            booking=booking,
            gym_config=gym_config,
        )
    elif status == "pending_payment":
        await BookingNotificationService.publish_pending_payment(
            db,
            tenant_id=tenant_id,
            booking=booking,
            gym_config=gym_config,
        )
    else:
        await BookingNotificationService.publish_for_booking(
            db,
            tenant_id=tenant_id,
            booking=booking,
            gym_config=gym_config,
        )
    return {
        "success": True,
        "message": "Booking created",
        "data": BookingCreatedData(
            booking_id=booking.id,
            status=booking.status,
            waiting_position=booking.waiting_position,
            payment_mode=booking.payment_mode,
            sessions_deducted=int(booking.sessions_deducted or 0),
        ),
    }


@router.post(
    "/{class_id}/bookings/waiting",
    response_model=BookingCreateResponse,
)
async def create_waiting_booking(
    class_id: uuid.UUID,
    body: BookingRequestBody,
    current_user: User = Depends(get_current_active_user),
    gym_config: GymConfigValue = Depends(get_gym_config_for_active_user),
    db: Session = Depends(get_db),
):
    """
    Add member to waitlist only when class is full.
    max_waitings controls how many waiting bookings are allowed.
    """
    tenant_id = current_user.tenant_id
    booking, wallet_txn_id = BookingsService.create(
        db,
        tenant_id,
        current_user,
        class_id,
        body.payment_mode,
        body.user_package_purchase_id,
        body.seat_id,
        body.notes,
        force_waiting=True,
        gym_config=gym_config,
    )
    db.commit()
    if wallet_txn_id is not None:
        await WalletNotificationService.publish_debited(
            db,
            tenant_id=tenant_id,
            wallet_transaction_id=wallet_txn_id,
        )
    await BookingNotificationService.publish_waitlist_joined(
        db,
        tenant_id=tenant_id,
        booking=booking,
        gym_config=gym_config,
    )
    return {
        "success": True,
        "message": "Added to waiting list",
        "data": BookingCreatedData(
            booking_id=booking.id,
            status=booking.status,
            waiting_position=booking.waiting_position,
            payment_mode=booking.payment_mode,
            sessions_deducted=int(booking.sessions_deducted or 0),
        ),
    }


@router.post(
    "/{class_id}/bookings/{booking_id}/cancel",
    response_model=BookingCancelResponse,
)
async def cancel_class_booking(
    class_id: uuid.UUID,
    booking_id: uuid.UUID,
    body: BookingCancelRequestBody,
    current_user: User = Depends(get_current_active_user),
    gym_config: GymConfigValue = Depends(get_gym_config_for_active_user),
    db: Session = Depends(get_db),
):
    tenant_id = current_user.tenant_id
    booking, promoted_booking = BookingsService.cancel(
        db=db,
        tenant_id=tenant_id,
        user=current_user,
        class_id=class_id,
        booking_id=booking_id,
        reason=body.reason,
        gym_config=gym_config,
    )
    db.commit()
    await BookingNotificationService.publish_cancelled(
        db,
        tenant_id=tenant_id,
        booking=booking,
        gym_config=gym_config,
    )
    if promoted_booking is not None:
        await BookingNotificationService.publish_waitlist_promoted(
            db,
            tenant_id=tenant_id,
            booking=promoted_booking,
            gym_config=gym_config,
        )
    gym_class = db.query(GymClass).filter(GymClass.id == class_id).first()
    return {
        "success": True,
        "message": "Booking cancelled",
        "data": BookingCancelledData(
            booking_id=booking.id,
            status=booking.status,
            cancelled_at=booking.cancelled_at.isoformat() if booking.cancelled_at else None,
            booking_counts=int(gym_class.booking_counts or 0) if gym_class else None,
        ),
    }
