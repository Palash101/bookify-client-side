import json
import logging
import uuid
from html import escape
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.settings import settings
from app.dependencies import (
    get_current_active_user,
    get_current_staff_or_trainer,
    get_current_tenant,
    get_current_tenant_id,
    get_gym_config_for_active_user,
)
from app.models.class_booking import class_booking_status_value
from app.models.gym_class import GymClass
from app.models.user import User
from app.schemas.booking import (
    AttendanceCheckInData,
    AttendanceCheckInRequestBody,
    AttendanceCheckInResponse,
    BookingCancelRequestBody,
    BookingCancelResponse,
    BookingCancelledData,
    BookingCreateResponse,
    BookingCreatedData,
    BookingQrData,
    BookingQrResponse,
    MemberBookingsResponse,
    BookingRequestBody,
    BookingValidateData,
    BookingValidateResponse,
)
from app.schemas.gym_config_value import GymConfigValue
from app.services.bookings_service import BookingsService
from app.services.notification_service import BookingNotificationService
from app.services.tenant_website_config_service import TenantWebsiteConfigService
from app.core.events.event_types import (
    CLIENT_BOOKING_CANCELLED,
    CLIENT_BOOKING_CONFIRMED,
    CLIENT_BOOKING_PENDING_PAYMENT,
    CLIENT_BOOKING_WAITLIST_JOINED,
    CLIENT_BOOKING_WAITLIST_PROMOTED,
    CLIENT_WALLET_DEBITED,
)
from app.services.pubsub_debug import publish_tenant_event_debug
from app.core.events.event_payloads import (
    build_booking_notification_data,
    build_wallet_notification_data,
)

router = APIRouter()
_log = logging.getLogger(__name__)


def _booking_qr_html(*, qr_svg: str, brand_name: str, logo_url: Optional[str], primary_color: str) -> str:
    safe_name = escape(brand_name or "Bookify")
    safe_logo = escape(logo_url) if logo_url else ""
    safe_primary = escape(primary_color or "#1d4ed8")
    logo_html = (
        f'<img src="{safe_logo}" alt="{safe_name} logo" style="width:92px;height:92px;object-fit:contain;border-radius:999px;background:#fff;padding:10px;box-shadow:0 6px 16px rgba(15,23,42,.12);" />'
        if safe_logo
        else f'<div style="width:92px;height:92px;border-radius:999px;background:#fff;display:flex;align-items:center;justify-content:center;color:{safe_primary};font-weight:700;box-shadow:0 6px 16px rgba(15,23,42,.12);">LOGO</div>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_name} QR Check-In</title>
</head>
<body style="margin:0;background:#f4f7fb;font-family:Inter,Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px;">
  <div style="width:100%;max-width:720px;background:#fff;border:12px solid {safe_primary};border-radius:38px;padding:32px;box-shadow:0 18px 40px rgba(15,23,42,.14);">
    <div style="display:flex;flex-direction:column;align-items:center;text-align:center;gap:12px;">
      {logo_html}
      <div style="font-size:32px;font-weight:800;color:#0f172a;">{safe_name}</div>
      <div style="font-size:18px;color:#475569;">Class Check-In</div>
    </div>
    <div style="margin:28px auto 24px;max-width:460px;background:#fff;border-radius:24px;padding:18px;box-shadow:inset 0 0 0 1px #e2e8f0;">
      {qr_svg}
    </div>
    <div style="margin:0 auto;max-width:420px;background:{safe_primary};color:#fff;border-radius:999px;padding:18px 24px;text-align:center;font-size:22px;font-weight:700;">
      Scan Me
    </div>
  </div>
</body>
</html>"""


def _commit_booking_or_raise(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _log.warning("booking_commit_integrity_error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not complete booking — seat may already be taken or data conflict.",
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        _log.exception("booking_commit_db_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save booking. Please try again.",
        ) from exc


def _persist_booking_write(db: Session, write_fn):
    """Run a booking write (flush inside service) then commit. Map DB conflicts to HTTP errors."""
    try:
        result = write_fn()
        _commit_booking_or_raise(db)
        return result
    except HTTPException:
        try:
            db.rollback()
        except Exception:
            pass
        raise


async def _publish_booking_event(
    db: Session,
    *,
    tenant_id: str,
    booking,
    event_type: str,
    gym_config: GymConfigValue,
) -> dict[str, str]:
    cfg = gym_config or GymConfigValue()
    if not BookingNotificationService._notification_enabled(cfg, event_type):
        _log.warning(
            "booking_gym_notification_off tenant_id=%s event_type=%s booking_id=%s",
            tenant_id,
            event_type,
            booking.id,
        )
    return await publish_tenant_event_debug(
        tenant_id=tenant_id,
        event_type=event_type,
        data=build_booking_notification_data(booking),
    )


async def _publish_wallet_debited(
    *,
    tenant_id: str,
    wallet_transaction_id: uuid.UUID,
) -> dict[str, str]:
    return await publish_tenant_event_debug(
        tenant_id=tenant_id,
        event_type=CLIENT_WALLET_DEBITED,
        data=build_wallet_notification_data(
            wallet_transaction_id=str(wallet_transaction_id),
        ),
    )


@router.get(
    "/bookings",
    response_model=MemberBookingsResponse,
)
async def get_member_bookings(
    current_user: User = Depends(get_current_active_user),
    gym_config: GymConfigValue = Depends(get_gym_config_for_active_user),
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    tenant_id = current_user.tenant_id
    return BookingsService.list_member_bookings(
        db,
        tenant_id,
        current_user,
        gym_config=gym_config,
        page=page,
        limit=limit,
    )


@router.get(
    "/{class_id}/bookings/{booking_id}/qr",
    response_model=BookingQrResponse,
)
async def get_booking_qr(
    request: Request,
    class_id: uuid.UUID,
    booking_id: uuid.UUID,
    token: Optional[str] = Query(None),
    current_tenant=Depends(get_current_tenant),
    tenant_id: str = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    qr_data = BookingsService.get_checkin_qr(
        db,
        tenant_id=tenant_id,
        class_id=class_id,
        booking_id=booking_id,
        access_token=token,
    )
    wants_html = "text/html" in (request.headers.get("accept") or "").lower()
    if wants_html:
        try:
            import qrcode
            import qrcode.image.svg
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="QR rendering dependency is not installed on the server",
            ) from exc

        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(qr_data["qr_token"], image_factory=factory, box_size=12, border=2)
        buffer = BytesIO()
        img.save(buffer)
        qr_svg = buffer.getvalue().decode("utf-8")

        website = TenantWebsiteConfigService.get_active_config(db, tenant_id=tenant_id)
        brand_name = (
            getattr(current_tenant, "name", None)
            or getattr(website, "theme_name", None)
            or "Bookify"
        )
        logo_url = getattr(website, "logo_url", None) if website else None
        primary_color = getattr(website, "primary_color", None) if website else None
        return HTMLResponse(
            content=_booking_qr_html(
                qr_svg=qr_svg,
                brand_name=brand_name,
                logo_url=logo_url,
                primary_color=primary_color or "#1d4ed8",
            )
        )
    return {
        "success": True,
        "message": "Booking QR fetched successfully",
        "data": BookingQrData(**qr_data),
    }


@router.get(
    "/{class_id}/bookings/{booking_id}/qr/view",
    response_class=HTMLResponse,
)
async def view_booking_qr(
    class_id: uuid.UUID,
    booking_id: uuid.UUID,
    token: Optional[str] = Query(None),
    current_tenant=Depends(get_current_tenant),
    tenant_id: str = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    qr_data = BookingsService.get_checkin_qr(
        db,
        tenant_id=tenant_id,
        class_id=class_id,
        booking_id=booking_id,
        access_token=token,
    )
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="QR rendering dependency is not installed on the server",
        ) from exc

    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(qr_data["qr_token"], image_factory=factory, box_size=12, border=2)
    buffer = BytesIO()
    img.save(buffer)
    qr_svg = buffer.getvalue().decode("utf-8")

    website = TenantWebsiteConfigService.get_active_config(db, tenant_id=tenant_id)
    brand_name = (
        getattr(current_tenant, "name", None)
        or getattr(website, "theme_name", None)
        or "Bookify"
    )
    logo_url = getattr(website, "logo_url", None) if website else None
    primary_color = getattr(website, "primary_color", None) if website else None
    return HTMLResponse(
        content=_booking_qr_html(
            qr_svg=qr_svg,
            brand_name=brand_name,
            logo_url=logo_url,
            primary_color=primary_color or "#1d4ed8",
        )
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
    booking, wallet_txn_id = _persist_booking_write(
        db,
        lambda: BookingsService.create(
            db,
            tenant_id,
            current_user,
            class_id,
            body.payment_mode,
            body.user_package_purchase_id,
            body.seat_id,
            body.notes,
            gym_config=gym_config,
        ),
    )
    pubsub: dict[str, dict[str, str]] = {}
    if wallet_txn_id is not None:
        pubsub["wallet_debited"] = await _publish_wallet_debited(
            tenant_id=tenant_id,
            wallet_transaction_id=wallet_txn_id,
        )
    status = class_booking_status_value(booking.status)
    if status == "confirmed":
        pubsub["booking"] = await _publish_booking_event(
            db,
            tenant_id=tenant_id,
            booking=booking,
            event_type=CLIENT_BOOKING_CONFIRMED,
            gym_config=gym_config,
        )
    elif status == "pending_payment":
        pubsub["booking"] = await _publish_booking_event(
            db,
            tenant_id=tenant_id,
            booking=booking,
            event_type=CLIENT_BOOKING_PENDING_PAYMENT,
            gym_config=gym_config,
        )
    else:
        event_type = BookingNotificationService.resolve_event_type(booking)
        if event_type:
            pubsub["booking"] = await _publish_booking_event(
                db,
                tenant_id=tenant_id,
                booking=booking,
                event_type=event_type,
                gym_config=gym_config,
            )
    return {
        "success": True,
        "message": "Booking created",
        "data": BookingCreatedData(
            booking_id=booking.id,
            booking_ref=booking.booking_ref,
            status=class_booking_status_value(booking.status),
            waiting_position=booking.waiting_position,
            payment_mode=booking.payment_mode,
            sessions_deducted=int(booking.sessions_deducted or 0),
            pubsub=pubsub or None,
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
    booking, wallet_txn_id = _persist_booking_write(
        db,
        lambda: BookingsService.create(
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
        ),
    )
    pubsub: dict[str, dict[str, str]] = {}
    if wallet_txn_id is not None:
        pubsub["wallet_debited"] = await _publish_wallet_debited(
            tenant_id=tenant_id,
            wallet_transaction_id=wallet_txn_id,
        )
    pubsub["booking"] = await _publish_booking_event(
        db,
        tenant_id=tenant_id,
        booking=booking,
        event_type=CLIENT_BOOKING_WAITLIST_JOINED,
        gym_config=gym_config,
    )
    return {
        "success": True,
        "message": "Added to waiting list",
        "data": BookingCreatedData(
            booking_id=booking.id,
            booking_ref=booking.booking_ref,
            status=class_booking_status_value(booking.status),
            waiting_position=booking.waiting_position,
            payment_mode=booking.payment_mode,
            sessions_deducted=int(booking.sessions_deducted or 0),
            pubsub=pubsub or None,
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
    booking, promoted_booking = _persist_booking_write(
        db,
        lambda: BookingsService.cancel(
            db=db,
            tenant_id=tenant_id,
            user=current_user,
            class_id=class_id,
            booking_id=booking_id,
            reason=body.reason,
            gym_config=gym_config,
        ),
    )
    pubsub: dict[str, dict[str, str]] = {
        "booking_cancelled": await _publish_booking_event(
            db,
            tenant_id=tenant_id,
            booking=booking,
            event_type=CLIENT_BOOKING_CANCELLED,
            gym_config=gym_config,
        ),
    }
    if promoted_booking is not None:
        pubsub["waitlist_promoted"] = await _publish_booking_event(
            db,
            tenant_id=tenant_id,
            booking=promoted_booking,
            event_type=CLIENT_BOOKING_WAITLIST_PROMOTED,
            gym_config=gym_config,
        )
    gym_class = db.query(GymClass).filter(GymClass.id == class_id).first()
    return {
        "success": True,
        "message": "Booking cancelled",
        "data": BookingCancelledData(
            booking_id=booking.id,
            status=class_booking_status_value(booking.status),
            cancelled_at=booking.cancelled_at.isoformat() if booking.cancelled_at else None,
            booking_counts=int(gym_class.booking_counts or 0) if gym_class else None,
        ),
    }


@router.post(
    "/{class_id}/attendance/checkin",
    response_model=AttendanceCheckInResponse,
)
async def checkin_class_attendance(
    class_id: uuid.UUID,
    body: AttendanceCheckInRequestBody,
    current_user: User = Depends(get_current_staff_or_trainer),
    db: Session = Depends(get_db),
):
    tenant_id = current_user.tenant_id
    data = _persist_booking_write(
        db,
        lambda: BookingsService.checkin_by_qr(
            db,
            tenant_id=tenant_id,
            scanner_user=current_user,
            class_id=class_id,
            qr_token=body.qr_token,
        ),
    )
    return {
        "success": True,
        "message": "Attendance already marked" if data["already_checked_in"] else "Attendance marked successfully",
        "data": AttendanceCheckInData(**data),
    }
