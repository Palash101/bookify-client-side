"""Build post-checkout redirect URLs for web clients and mobile deep links."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

from app.core.db.master_db import SessionLocal
from app.core.settings import settings
from app.models.master_org import Organization

_WEB_SUCCESS_PATH = "/payment-success"
_WEB_FAILED_PATH = "/payment-failed"


def normalize_checkout_platform(value: Any) -> str:
    platform = str(value or "web").strip().lower()
    return platform if platform in ("web", "app") else "web"


def checkout_platform_from_metadata(meta: Optional[dict[str, Any]]) -> str:
    meta = meta or {}
    return normalize_checkout_platform(
        meta.get("checkout_platform") or meta.get("platform")
    )


def attach_checkout_platform_debug(
    debug: dict[str, str],
    meta: Optional[dict[str, Any]],
) -> None:
    debug["checkout_platform"] = checkout_platform_from_metadata(meta)


def tenant_web_origin(tenant_id: Optional[str]) -> Optional[str]:
    """Resolve https://{organization.domain} for the tenant, if configured."""
    if not tenant_id:
        return None
    db = SessionLocal()
    try:
        org = (
            db.query(Organization)
            .filter(Organization.organization_id == tenant_id)
            .first()
        )
        if org and org.domain:
            domain = str(org.domain).strip().rstrip("/")
            if domain.startswith("http://") or domain.startswith("https://"):
                return domain
            return f"https://{domain}"
    finally:
        db.close()
    return None


def web_origin_for_tenant(tenant_id: Optional[str]) -> str:
    """Tenant domain when present, otherwise default web origin."""
    return tenant_web_origin(tenant_id) or settings.PAYMENT_WEB_ORIGIN.rstrip("/")


def build_client_return_url(
    *,
    platform: str,
    session_id: str,
    success: bool,
    tenant_id: Optional[str] = None,
    extra: Optional[dict[str, str]] = None,
) -> str:
    """
    After API processes Stripe redirect, send the user back to web or app.

    Web (tenant domain when known, else PAYMENT_WEB_ORIGIN):
      https://{domain}/payment-success?session_id=...&status=success
      https://{domain}/payment-failed?session_id=...&status=cancelled
    App:
      bookify://payment/success?session_id=...&status=success
    """
    params: dict[str, str] = {"session_id": session_id}
    has_error = bool(extra and extra.get("error"))
    if has_error:
        params["status"] = "error"
        params["error"] = extra["error"]  # type: ignore[index]
    else:
        params["status"] = "success" if success else "cancelled"
    if extra:
        for key in ("sale_id", "order_id"):
            if extra.get(key):
                params[key] = extra[key]

    platform_norm = normalize_checkout_platform(platform)
    if platform_norm == "app":
        if params.get("status") == "cancelled":
            base = settings.PAYMENT_CANCEL_DEEP_LINK
        else:
            base = settings.PAYMENT_SUCCESS_DEEP_LINK
        return f"{base.rstrip('/')}?{urlencode(params)}"

    origin = web_origin_for_tenant(tenant_id)
    path = _WEB_SUCCESS_PATH if (success and not has_error) else _WEB_FAILED_PATH
    return f"{origin}{path}?{urlencode(params)}"
