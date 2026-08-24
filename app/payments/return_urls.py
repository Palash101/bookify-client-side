"""Build post-checkout redirect URLs for web clients and mobile deep links."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from app.core.db.master_db import SessionLocal
from app.core.settings import settings
from app.models.master_org import Organization

_WEB_SUCCESS_PATH = "/payment-success"
_WEB_FAILED_PATH = "/payment-failed"
_LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1"})


def normalize_checkout_platform(value: Any) -> str:
    platform = str(value or "web").strip().lower()
    return platform if platform in ("web", "app") else "web"


def checkout_platform_from_metadata(meta: Optional[dict[str, Any]]) -> str:
    meta = meta or {}
    return normalize_checkout_platform(
        meta.get("checkout_platform") or meta.get("platform")
    )


def parse_web_origin(raw: Optional[str]) -> Optional[str]:
    """Normalize Origin/Referer/domain to ``scheme://host`` (no path)."""
    value = str(raw or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    host = (parsed.hostname or "").strip(".").lower()
    if not host:
        return None
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    if parsed.port:
        return f"{scheme}://{host}:{parsed.port}"
    return f"{scheme}://{host}"


def checkout_origin_from_request(request: Any) -> Optional[str]:
    """Browser site that started checkout (Origin, else Referer)."""
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    for header in ("origin", "referer"):
        origin = parse_web_origin(headers.get(header))
        if origin:
            return origin
    return None


def checkout_origin_metadata(request: Any) -> dict[str, str]:
    origin = checkout_origin_from_request(request)
    return {"checkout_origin": origin} if origin else {}


def _platform_base_domain() -> str:
    return str(settings.PAYMENT_TENANT_BASE_DOMAIN or "").strip().lstrip(".").lower()


def is_allowed_checkout_origin(origin: str, tenant_id: Optional[str] = None) -> bool:
    """Block open redirects; allow the page the user actually paid from."""
    parsed = urlparse(origin)
    host = (parsed.hostname or "").strip(".").lower()
    scheme = parsed.scheme
    if not host or scheme not in ("http", "https"):
        return False
    if scheme == "http" and host not in _LOCAL_HTTP_HOSTS:
        return False

    base = _platform_base_domain()
    if base and scheme == "https" and (host == base or host.endswith(f".{base}")):
        return True

    tenant_origin = tenant_web_origin(tenant_id) if tenant_id else None
    if tenant_origin and origin.rstrip("/") == tenant_origin.rstrip("/"):
        return True

    for allowed in settings.BACKEND_CORS_ORIGINS or []:
        if parse_web_origin(allowed) == origin:
            return True

    return False


def attach_checkout_platform_debug(
    debug: dict[str, str],
    meta: Optional[dict[str, Any]],
) -> None:
    meta = meta or {}
    debug["checkout_platform"] = checkout_platform_from_metadata(meta)
    origin = parse_web_origin(meta.get("checkout_origin"))
    if origin:
        debug["checkout_origin"] = origin


def origin_from_org_domain(raw: Optional[str]) -> Optional[str]:
    """Turn organizations.domain into a public https origin.

    Master DB stores a FQDN (``neha.fitnezstudios.com``, ``club.studio``) or a
    tenant slug (``powergym``). A slug is not a resolvable hostname, so it
    becomes ``https://{slug}.{PAYMENT_TENANT_BASE_DOMAIN}``.
    """
    parsed_origin = parse_web_origin(raw)
    if not parsed_origin:
        return None
    parsed = urlparse(parsed_origin)
    host = (parsed.hostname or "").strip(".").lower()
    if not host:
        return None

    if "." not in host:
        base = _platform_base_domain()
        if not base:
            return None
        host = f"{host}.{base}"

    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    if parsed.port:
        return f"{scheme}://{host}:{parsed.port}"
    return f"{scheme}://{host}"


def tenant_web_origin(tenant_id: Optional[str]) -> Optional[str]:
    """Resolve the gym website origin for the tenant, if configured."""
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
            return origin_from_org_domain(str(org.domain))
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

    Web prefers the Origin that started checkout (so cc.fitnezstudios.com
    does not bounce to powergym.fitnezstudios.com). Fallback is the tenant
    Organization.domain (FQDN or {slug}.fitnezstudios.com), else PAYMENT_WEB_ORIGIN:
      https://{host}/payment-success?session_id=...&status=success
      https://{host}/payment-failed?session_id=...&status=cancelled
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
    started_on = parse_web_origin((extra or {}).get("checkout_origin"))
    if started_on and is_allowed_checkout_origin(started_on, tenant_id):
        origin = started_on
    path = _WEB_SUCCESS_PATH if (success and not has_error) else _WEB_FAILED_PATH
    return f"{origin}{path}?{urlencode(params)}"
