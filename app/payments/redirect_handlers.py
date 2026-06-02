"""Stripe/browser redirect handlers for payment success and cancel."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi.responses import JSONResponse

from app.core.db.session import get_session_factory
from app.payments.tenant_resolve import resolve_tenant_for_stripe_session
from app.services.payment_success_service import PaymentSuccessService

logger = logging.getLogger(__name__)


def payment_redirect_base_url(public_api_base: str) -> str:
    """API root for payment redirects, e.g. https://api.example.com/api/v1/client."""
    return f"{public_api_base.rstrip('/')}"


def payment_success_urls(public_api_base: str) -> dict[str, str]:
    base = payment_redirect_base_url(public_api_base)
    return {
        "callback_base_url": base,
        "success_url": f"{base}/payment/success",
        "cancel_url": f"{base}/payment/cancel",
    }


def build_payment_success_response(session_id: Optional[str]) -> JSONResponse:
    def _respond(**payload: Optional[str]) -> JSONResponse:
        clean = {k: str(v) for k, v in payload.items() if v is not None and str(v) != ""}
        return JSONResponse(
            status_code=200,
            content={
                "success": "error" not in clean,
                "message": clean.get("error") or "Payment success received",
                **clean,
            },
        )

    if not session_id:
        return _respond(error="missing_session_id")

    tenant_id = resolve_tenant_for_stripe_session(session_id)
    if not tenant_id:
        return _respond(error="unable_to_resolve_tenant", session_id=session_id)

    db = get_session_factory(tenant_id)()
    try:
        debug = PaymentSuccessService.handle(db, session_id)
        if debug.get("error"):
            db.rollback()
            return _respond(error=debug["error"], **debug)
        db.commit()
        return _respond(**debug)
    except Exception:
        logger.exception(
            "payment_success failed (session_id=%s, tenant_id=%s)",
            session_id,
            tenant_id,
        )
        db.rollback()
        return _respond(error="payment_success_failed", session_id=session_id)
    finally:
        db.close()


def build_payment_cancel_response(session_id: Optional[str]) -> dict:
    return {
        "success": False,
        "message": "Payment cancelled redirect received",
        "session_id": session_id,
    }
