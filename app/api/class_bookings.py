import json
import logging
import sys
import uuid
import base64
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, Response
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
from app.core.mailer import email_service
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


def _public_booking_qr_url(*, class_id: uuid.UUID, booking_id: uuid.UUID, tenant_id: str, token: str) -> str:
    base = f"https://api.{settings.PAYMENT_TENANT_BASE_DOMAIN}".rstrip("/")
    return (
        f"{base}{settings.API_V1_STR}/classes/{class_id}/bookings/{booking_id}/qr"
        f"?tenant_id={tenant_id}&token={token}"
    )


async def _send_booking_qr_email(
    *,
    db: Session,
    tenant_id: str,
    booking,
    current_user: User,
) -> None:
    if not current_user.email:
        return
    qr_data = BookingsService.get_checkin_qr(
        db,
        tenant_id=tenant_id,
        class_id=booking.class_id,
        booking_id=booking.id,
    )
    website = TenantWebsiteConfigService.get_active_config(db, tenant_id=tenant_id)
    brand_name = getattr(website, "theme_name", None) or settings.PROJECT_NAME
    primary_color = _safe_hex_color(getattr(website, "primary_color", None), "#1d4ed8")
    qr_url = _public_booking_qr_url(
        class_id=booking.class_id,
        booking_id=booking.id,
        tenant_id=tenant_id,
        token=qr_data["qr_token"],
    )
    html_body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background:#f8fafc; padding:24px; color:#0f172a;">
        <div style="max-width:640px; margin:0 auto; background:#ffffff; border:1px solid #e2e8f0; border-radius:18px; padding:28px;">
          <h2 style="margin:0 0 12px;">Your Booking QR</h2>
          <p style="margin:0 0 18px;">Your class booking is confirmed. Show this QR at check-in.</p>
          <div style="text-align:center; margin:24px 0;">
            <img src="{qr_url}" alt="Booking QR" style="max-width:320px; width:100%; border-radius:24px; border:10px solid {primary_color};" />
          </div>
          <p style="margin:0 0 8px;"><strong>Booking Ref:</strong> {booking.booking_ref or "-"}</p>
          <p style="margin:0 0 20px;"><strong>Gym:</strong> {brand_name}</p>
          <p style="margin:0;">
            <a href="{qr_url}" style="display:inline-block; background:{primary_color}; color:#ffffff; text-decoration:none; padding:12px 20px; border-radius:999px;">Open QR</a>
          </p>
        </div>
      </body>
    </html>
    """
    await email_service.send_email(
        subject="Your Booking QR Code",
        recipients=[current_user.email],
        body="Your booking QR code is ready.",
        html_body=html_body,
    )


def _booking_qr_html(*, qr_svg: str, brand_name: str, logo_url: Optional[str], primary_color: str) -> str:
    safe_name = escape(brand_name or "Bookify")
    safe_logo = escape(logo_url) if logo_url else ""
    safe_primary = escape(primary_color or "#1d4ed8")
    phone_icon = f"""
<svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true">
  <rect x="7" y="2.5" width="10" height="19" rx="2.5" stroke="white" stroke-width="2"/>
  <circle cx="12" cy="18" r="1.2" fill="white"/>
  <rect x="10" y="5" width="4" height="1.8" rx="0.9" fill="white"/>
</svg>
"""
    center_logo_html = (
        f'<img src="{safe_logo}" alt="{safe_name} logo" style="width:132px;height:132px;object-fit:contain;border-radius:999px;background:#fff;padding:12px;box-shadow:0 10px 26px rgba(15,23,42,.18);" />'
        if safe_logo
        else f'<div style="width:132px;height:132px;border-radius:999px;background:#fff;display:flex;align-items:center;justify-content:center;color:{safe_primary};font-weight:700;box-shadow:0 10px 26px rgba(15,23,42,.18);">LOGO</div>'
    )
    marker = lambda position: f"""
<div style="position:absolute;{position};width:96px;height:96px;border-radius:24px;background:#0f172a;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 14px rgba(15,23,42,.08);">
  <div style="width:64px;height:64px;border-radius:18px;background:#fff;display:flex;align-items:center;justify-content:center;">
    <div style="width:40px;height:40px;border-radius:10px;background:{safe_primary};"></div>
  </div>
</div>
"""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_name} QR Check-In</title>
</head>
<body style="margin:0;background:#f4f7fb;font-family:Inter,Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px;">
  <div style="position:relative;width:100%;max-width:820px;background:#fff;border:14px solid {safe_primary};border-radius:48px;padding:38px 38px 28px;box-shadow:0 18px 40px rgba(15,23,42,.14);">
    <div style="position:absolute;left:32px;bottom:20px;width:96px;height:8px;border-radius:999px;background:{safe_primary};"></div>
    <div style="position:absolute;right:32px;bottom:20px;width:96px;height:8px;border-radius:999px;background:{safe_primary};"></div>
    <div style="position:relative;margin:0 auto 22px;width:560px;height:560px;background:#fff;border-radius:32px;padding:22px;box-shadow:inset 0 0 0 1px #e2e8f0;overflow:hidden;">
      {qr_svg}
      {marker("top:22px;left:22px;")}
      {marker("top:22px;right:22px;")}
      {marker("bottom:22px;left:22px;")}
      <div style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);display:flex;align-items:center;justify-content:center;">
        <div style="border:6px solid {safe_primary};border-radius:999px;padding:4px;background:#fff;">{center_logo_html}</div>
      </div>
    </div>
    <div style="margin:0 auto;max-width:440px;background:{safe_primary};color:#fff;border-radius:999px;padding:16px 24px;text-align:center;font-size:22px;font-weight:700;box-shadow:0 10px 22px rgba(15,23,42,.12);display:flex;align-items:center;justify-content:center;gap:14px;">
      {phone_icon}
      <span style="display:inline-block;width:1px;height:26px;background:rgba(255,255,255,.5);"></span>
      <span>Scan Me</span>
    </div>
  </div>
</body>
</html>"""


def _safe_hex_color(raw: Optional[str], default: str) -> str:
    value = str(raw or "").strip()
    if len(value) == 7 and value.startswith("#"):
        return value
    return default


def _build_branded_qr_png(
    *,
    qr_value: str,
    brand_name: str,
    logo_url: Optional[str],
    primary_color: Optional[str],
) -> bytes:
    import httpx
    vendor_path = Path(__file__).resolve().parents[1].parent / ".vendor_qr"
    try:
        import qrcode
    except ImportError:
        if str(vendor_path) not in sys.path:
            sys.path.insert(0, str(vendor_path))
        import qrcode
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        if str(vendor_path) not in sys.path:
            sys.path.insert(0, str(vendor_path))
        from PIL import Image, ImageDraw

    card_bg = "#ffffff"
    canvas_bg = "#f4f7fb"
    accent = _safe_hex_color(primary_color, "#1d4ed8")
    dark = "#0f172a"

    width, height = 900, 1180
    card_margin = 48
    card_radius = 42
    border_width = 14

    img = Image.new("RGBA", (width, height), canvas_bg)
    draw = ImageDraw.Draw(img)

    card_box = (card_margin, card_margin, width - card_margin, height - card_margin)
    draw.rounded_rectangle(card_box, radius=card_radius, fill=card_bg, outline=accent, width=border_width)

    qr = qrcode.QRCode(box_size=12, border=4)
    qr.add_data(qr_value)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    qr_img = qr_img.resize((520, 520))

    qr_wrap = Image.new("RGBA", (600, 600), "white")
    qr_wrap_draw = ImageDraw.Draw(qr_wrap)
    qr_wrap_draw.rounded_rectangle((0, 0, 599, 599), radius=28, fill="white", outline="#e5e7eb", width=2)
    qr_wrap.alpha_composite(qr_img, ((600 - 520) // 2, (600 - 520) // 2))
    img.alpha_composite(qr_wrap, ((width - 600) // 2, 150))

    logo_size = 148
    logo_badge = Image.new("RGBA", (logo_size, logo_size), (255, 255, 255, 0))
    badge_draw = ImageDraw.Draw(logo_badge)
    badge_draw.ellipse((0, 0, logo_size - 1, logo_size - 1), fill="white", outline=accent, width=5)

    pasted_logo = False
    if logo_url:
        try:
            resp = httpx.get(logo_url, timeout=5.0, follow_redirects=True)
            if resp.status_code == 200:
                raw_logo = Image.open(BytesIO(resp.content)).convert("RGBA")
                raw_logo.thumbnail((92, 92))
                logo_x = (logo_size - raw_logo.width) // 2
                logo_y = (logo_size - raw_logo.height) // 2
                logo_badge.alpha_composite(raw_logo, (logo_x, logo_y))
                pasted_logo = True
        except Exception:
            pasted_logo = False

    if not pasted_logo:
        badge_draw.ellipse((34, 34, logo_size - 35, logo_size - 35), fill=accent)

    badge_x = (width - logo_size) // 2
    badge_y = 150 + (600 - logo_size) // 2
    img.alpha_composite(logo_badge, (badge_x, badge_y))

    footer_left = 220
    footer_top = 860
    footer_right = width - 220
    footer_bottom = 930
    draw.rounded_rectangle(
        (footer_left, footer_top, footer_right, footer_bottom),
        radius=35,
        fill=accent,
    )
    # phone icon
    phone_x = footer_left + 110
    phone_y = footer_top + 16
    draw.rounded_rectangle((phone_x, phone_y, phone_x + 26, phone_y + 38), radius=6, outline="white", width=3)
    draw.rounded_rectangle((phone_x + 8, phone_y + 5, phone_x + 18, phone_y + 8), radius=2, fill="white")
    draw.ellipse((phone_x + 10, phone_y + 28, phone_x + 16, phone_y + 34), fill="white")
    draw.line((phone_x + 54, footer_top + 12, phone_x + 54, footer_bottom - 12), fill=(255, 255, 255, 150), width=2)

    # simple text without custom font dependency
    draw.text((phone_x + 88, footer_top + 20), "Scan Me", fill="white")
    draw.text((width // 2 - 40, 770), brand_name[:24], fill=dark)

    draw.rounded_rectangle((86, height - 86, 150, height - 78), radius=4, fill=accent)
    draw.rounded_rectangle((width - 150, height - 86, width - 86, height - 78), radius=4, fill=accent)

    output = BytesIO()
    img.convert("RGB").save(output, format="PNG")
    return output.getvalue()


def _build_branded_qr_svg(
    *,
    qr_value: str,
    brand_name: str,
    logo_url: Optional[str],
    primary_color: Optional[str],
) -> str:
    vendor_path = Path(__file__).resolve().parents[1].parent / ".vendor_qr"
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        if str(vendor_path) not in sys.path:
            sys.path.insert(0, str(vendor_path))
        import qrcode
        import qrcode.image.svg

    accent = _safe_hex_color(primary_color, "#1d4ed8")
    safe_name = escape(brand_name or "Bookify")
    safe_logo = escape(logo_url) if logo_url else ""

    factory = qrcode.image.svg.SvgPathImage
    qr_img = qrcode.make(qr_value, image_factory=factory, box_size=10, border=4)
    buffer = BytesIO()
    qr_img.save(buffer)
    qr_svg = buffer.getvalue().decode("utf-8")
    qr_data_uri = "data:image/svg+xml;base64," + base64.b64encode(qr_svg.encode("utf-8")).decode("ascii")

    logo_fragment = ""
    if safe_logo:
        logo_fragment = (
            f'<image href="{safe_logo}" x="398" y="378" width="104" height="104" preserveAspectRatio="xMidYMid meet" />'
        )
    else:
        logo_fragment = '<text x="450" y="440" text-anchor="middle" font-size="22" font-family="Arial" fill="#0f172a">LOGO</text>'

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="900" height="1180" viewBox="0 0 900 1180">
  <rect width="900" height="1180" fill="#f4f7fb"/>
  <rect x="48" y="48" width="804" height="1084" rx="42" fill="#ffffff" stroke="{accent}" stroke-width="14"/>
  <rect x="150" y="168" width="600" height="600" rx="28" fill="#ffffff" stroke="#e5e7eb" stroke-width="2"/>
  <image href="{qr_data_uri}" x="190" y="208" width="520" height="520" preserveAspectRatio="xMidYMid meet" />
  <circle cx="450" cy="430" r="82" fill="#ffffff" stroke="{accent}" stroke-width="6"/>
  {logo_fragment}
  <text x="450" y="805" text-anchor="middle" font-size="30" font-family="Arial, sans-serif" font-weight="700" fill="#0f172a">{safe_name}</text>
  <rect x="230" y="870" width="440" height="78" rx="39" fill="{accent}"/>
  <rect x="330" y="890" width="24" height="34" rx="5" fill="none" stroke="#ffffff" stroke-width="3"/>
  <rect x="338" y="896" width="8" height="3" rx="1.5" fill="#ffffff"/>
  <circle cx="342" cy="916" r="3" fill="#ffffff"/>
  <line x1="386" y1="886" x2="386" y2="932" stroke="#ffffff" stroke-opacity="0.55" stroke-width="2"/>
  <text x="505" y="919" text-anchor="middle" font-size="24" font-family="Arial, sans-serif" font-weight="700" fill="#ffffff">Scan Me</text>
  <rect x="86" y="1080" width="96" height="8" rx="4" fill="{accent}"/>
  <rect x="718" y="1080" width="96" height="8" rx="4" fill="{accent}"/>
</svg>"""


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
        website = TenantWebsiteConfigService.get_active_config(db, tenant_id=tenant_id)
        brand_name = (
            getattr(current_tenant, "name", None)
            or getattr(website, "theme_name", None)
            or "Bookify"
        )
        logo_url = getattr(website, "logo_url", None) if website else None
        primary_color = getattr(website, "primary_color", None) if website else None
        return Response(
            content=_build_branded_qr_svg(
                qr_value=qr_data["qr_token"],
                brand_name=brand_name,
                logo_url=logo_url,
                primary_color=primary_color,
            ),
            media_type="image/svg+xml",
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
    website = TenantWebsiteConfigService.get_active_config(db, tenant_id=tenant_id)
    brand_name = (
        getattr(current_tenant, "name", None)
        or getattr(website, "theme_name", None)
        or "Bookify"
    )
    logo_url = getattr(website, "logo_url", None) if website else None
    primary_color = getattr(website, "primary_color", None) if website else None
    return Response(
        content=_build_branded_qr_svg(
            qr_value=qr_data["qr_token"],
            brand_name=brand_name,
            logo_url=logo_url,
            primary_color=primary_color,
        ),
        media_type="image/svg+xml",
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
        try:
            await _send_booking_qr_email(
                db=db,
                tenant_id=tenant_id,
                booking=booking,
                current_user=current_user,
            )
        except Exception:
            _log.exception("booking_qr_email_failed booking_id=%s", booking.id)
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
