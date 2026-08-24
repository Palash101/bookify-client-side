"""Stripe/browser redirect handlers for payment success and cancel."""

from __future__ import annotations

import logging
from typing import Optional, Union

from fastapi.responses import JSONResponse, RedirectResponse

from app.core.db.session import get_session_factory
from app.payments.return_urls import build_client_return_url, normalize_checkout_platform, web_origin_for_tenant
from app.payments.tenant_resolve import resolve_tenant_for_stripe_session
from app.services.payment_cancel_service import PaymentCancelService
from app.services.payment_success_service import PaymentSuccessService
from app.services.package_notification_service import PackageNotificationService
from app.services.payment_notification_service import PaymentNotificationService
from app.services.wallet_notification_service import (
    WalletNotificationService,
    mark_wallet_topup_email_sent,
    wallet_topup_email_pending,
)
from app.models.sales import Sale
from app.services.event_publish_service import PublishedEvent

logger = logging.getLogger(__name__)


def _client_redirect(
    *,
    session_id: str,
    success: bool,
    payload: dict[str, str],
    tenant_id: Optional[str] = None,
) -> RedirectResponse:
    platform = normalize_checkout_platform(payload.get("checkout_platform"))
    url = build_client_return_url(
        platform=platform,
        session_id=session_id,
        success=success,
        tenant_id=tenant_id or payload.get("tenant_id"),
        extra=payload,
    )
    return RedirectResponse(url=url, status_code=302)


def _pubsub_debug_fields(
    debug: dict[str, str],
    *,
    prefix: str,
    published: Optional[PublishedEvent],
    skipped_reason: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    if skipped_reason:
        debug[f"{prefix}_pubsub_skipped"] = skipped_reason
    if error:
        debug[f"{prefix}_pubsub_error"] = error
    if published is not None:
        debug[f"{prefix}_pubsub_event_type"] = published.event_type
        debug[f"{prefix}_pubsub_event_id"] = published.event_id
        debug[f"{prefix}_pubsub_message_id"] = published.message_id


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


async def build_payment_success_response(
    session_id: Optional[str],
) -> Union[RedirectResponse, JSONResponse]:
    def _json(**payload: Optional[str]) -> JSONResponse:
        clean = {k: str(v) for k, v in payload.items() if v is not None and str(v) != ""}
        return JSONResponse(
            status_code=200,
            content={
                "success": "error" not in clean,
                "message": clean.get("error") or "Payment success received",
                **clean,
            },
        )

    tenant_id: Optional[str] = None

    def _respond(**payload: Optional[str]) -> Union[RedirectResponse, JSONResponse]:
        clean = {k: str(v) for k, v in payload.items() if v is not None and str(v) != ""}
        if session_id:
            return _client_redirect(
                session_id=session_id,
                success=True,
                payload=clean,
                tenant_id=tenant_id,
            )
        return _json(**clean)

    if not session_id:
        return _json(error="missing_session_id")

    tenant_id = resolve_tenant_for_stripe_session(session_id)
    if not tenant_id:
        return _respond(error="unable_to_resolve_tenant", session_id=session_id)

    try:
        db = get_session_factory(tenant_id)()
    except RuntimeError:
        return _respond(
            error="unable_to_open_tenant_db",
            session_id=session_id,
            tenant_id=tenant_id,
        )
    try:
        debug = PaymentSuccessService.handle(db, session_id)
        if debug.get("error"):
            db.rollback()
            return _respond(error=debug["error"], **debug)
        db.commit()
        event_tenant_id = tenant_id
        topup_wallet_txn_id = debug.get("wallet_topup_wallet_transaction_id")
        if topup_wallet_txn_id:
            sale = (
                db.query(Sale)
                .filter(Sale.wallet_transaction_id == topup_wallet_txn_id)
                .first()
            )
            if sale is not None and not wallet_topup_email_pending(sale):
                _pubsub_debug_fields(debug, prefix="wallet_topup", skipped_reason="already_sent")
            else:
                try:
                    published = await WalletNotificationService.publish_topup_success(
                        db,
                        tenant_id=event_tenant_id,
                        wallet_transaction_id=topup_wallet_txn_id,
                    )
                    _pubsub_debug_fields(debug, prefix="wallet_topup", published=published)
                    if published is not None and sale is not None:
                        mark_wallet_topup_email_sent(sale)
                        db.commit()
                except Exception as exc:
                    _pubsub_debug_fields(
                        debug,
                        prefix="wallet_topup",
                        error=str(exc),
                    )
                    logger.exception(
                        "payment_success topup notification failed (session_id=%s, event_tenant_id=%s)",
                        session_id,
                        event_tenant_id,
                    )
        package_purchased_user_package_id = debug.get("package_purchased_user_package_id")
        if package_purchased_user_package_id:
            try:
                published = await PackageNotificationService.publish_purchased(
                    db,
                    tenant_id=event_tenant_id,
                    user_package_id=package_purchased_user_package_id,
                )
                _pubsub_debug_fields(debug, prefix="package", published=published)
            except Exception as exc:
                _pubsub_debug_fields(debug, prefix="package", error=str(exc))
                logger.exception(
                    "payment_success package notification failed (session_id=%s, event_tenant_id=%s)",
                    session_id,
                    event_tenant_id,
                )
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


async def build_payment_cancel_response(
    session_id: Optional[str],
) -> Union[RedirectResponse, JSONResponse]:
    def _json(**payload: Optional[str]) -> JSONResponse:
        clean = {k: str(v) for k, v in payload.items() if v is not None and str(v) != ""}
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "message": "Payment cancelled redirect received",
                **clean,
            },
        )

    tenant_id: Optional[str] = None

    def _respond(**payload: Optional[str]) -> Union[RedirectResponse, JSONResponse]:
        clean = {k: str(v) for k, v in payload.items() if v is not None and str(v) != ""}
        if session_id:
            return _client_redirect(
                session_id=session_id,
                success=False,
                payload=clean,
                tenant_id=tenant_id,
            )
        return _json(**clean)

    if not session_id:
        # Stripe/PayPal redirect without session id, or someone opened this URL directly.
        origin = web_origin_for_tenant(None)
        return RedirectResponse(
            url=f"{origin}/payment-failed?status=cancelled&error=missing_session_id",
            status_code=302,
        )

    tenant_id = resolve_tenant_for_stripe_session(session_id)
    if not tenant_id:
        return _respond(error="unable_to_resolve_tenant", session_id=session_id)

    db = get_session_factory(tenant_id)()
    try:
        debug = PaymentCancelService.handle(db, session_id)
        if debug.get("error") and "payment_failed_sales_transaction_id" not in debug:
            db.rollback()
            return _respond(**debug)
        db.commit()
        event_tenant_id = tenant_id
        payment_failed_sales_transaction_id = debug.get("payment_failed_sales_transaction_id")
        if payment_failed_sales_transaction_id:
            try:
                published = await PaymentNotificationService.publish_failed(
                    db,
                    tenant_id=event_tenant_id,
                    sales_transaction_id=payment_failed_sales_transaction_id,
                )
                _pubsub_debug_fields(debug, prefix="payment_failed", published=published)
            except Exception as exc:
                _pubsub_debug_fields(debug, prefix="payment_failed", error=str(exc))
                logger.exception(
                    "payment_cancel notification failed (session_id=%s, event_tenant_id=%s)",
                    session_id,
                    event_tenant_id,
                )
        return _respond(**debug)
    except Exception:
        logger.exception(
            "payment_cancel failed (session_id=%s, tenant_id=%s)",
            session_id,
            tenant_id,
        )
        db.rollback()
        return _respond(error="payment_cancel_failed", session_id=session_id)
    finally:
        db.close()
