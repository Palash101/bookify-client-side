from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware as FastAPICORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.orm import Session
from app.core.db.master_db import SessionLocal
from app.models.master_org import Organization
from app.models.master_org_apikey import APIKeyStatus, OrganizationAPIKey
from app.core.settings import settings
import logging
import threading
import time
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_api_prefix = settings.API_V1_STR.rstrip("/")

EXCLUDED_PATHS = [
    "/health",
    f"{_api_prefix}/docs",
    f"{_api_prefix}/redoc",
    f"{_api_prefix}/openapi.json",
]


def _extract_request_domain(request: Request) -> Optional[str]:
    """
    Resolve the originating domain of the *client* that issued the request.

    For browser clients the calling site's domain is carried by the `Origin`
    header (falling back to `Referer`). For example, a page served from
    `https://velo.fitnezstudios.com` calling `https://api.fitnezstudios.com`
    sends `Origin: https://velo.fitnezstudios.com`.

    The `Host` / `X-Forwarded-Host` headers reflect the API endpoint that was
    *dialled* (`api.fitnezstudios.com`), not the calling site, so they are only
    used as a last-resort fallback for non-browser callers.

    The port is stripped so lookups match the value stored on
    `Organization.domain`.
    """
    # Preferred: the frontend origin reported by the browser.
    for header in ("origin", "referer"):
        raw = request.headers.get(header)
        if not raw:
            continue
        host = urlparse(raw).hostname
        if host:
            return host.lower()

    # Fallback for non-browser callers that don't send Origin/Referer.
    raw = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not raw:
        return None
    # X-Forwarded-Host may contain a comma-separated list; take the first entry.
    host = raw.split(",")[0].strip()
    if not host:
        return None
    # Strip port if present.
    if ":" in host:
        host = host.split(":", 1)[0]
    return host.lower() or None


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"success": False, "message": message, "detail": message},
    )


class TenantMiddleware(BaseHTTPMiddleware):
    """
    Middleware that resolves the active organization for every API request.

    A request is accepted if either:
      - `X-Tenant-Key` header is present and matches an active `OrganizationAPIKey`
        whose linked `Organization` is active, or
      - the request domain matches an active `Organization.domain`.

    On success, `request.state.organization_id` and `request.state.organization`
    are populated. If neither identifier is present, the request is rejected with 401.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if request.method == "OPTIONS":
            return await call_next(request)

        if (
            path in EXCLUDED_PATHS
            or path.startswith(f"{_api_prefix}/docs")
            or path.startswith(f"{_api_prefix}/redoc")
        ):
            return await call_next(request)

        # Payment redirects & webhooks (no tenant header — called by Stripe/browser).
        if path in (
            f"{_api_prefix}/payment/success",
            f"{_api_prefix}/payment/cancel",
        ) or path.startswith(
            (
                f"{_api_prefix}/payment/callback/",
                f"{_api_prefix}/callback/",
            )
        ):
            return await call_next(request)

        if not path.startswith("/api/"):
            return await call_next(request)

        x_tenant_key = request.headers.get("X-Tenant-Key")
        print(x_tenant_key,'x_tenant_key')
        request_domain = _extract_request_domain(request)
        print(request_domain,'request_domain')

        if not x_tenant_key and not request_domain:
            return _unauthorized(
                "Either X-Tenant-Key header or a request domain is required"
            )

        db: Session = SessionLocal()
        try:
            organization: Optional[Organization] = None

            if x_tenant_key:
                api_key = (
                    db.query(OrganizationAPIKey)
                    .filter(
                        OrganizationAPIKey.api_key == x_tenant_key,
                        OrganizationAPIKey.status == APIKeyStatus.active,
                    )
                    .first()
                )

                if not api_key:
                    return _unauthorized("Invalid or inactive organization API key")

                organization = (
                    db.query(Organization)
                    .filter(
                        Organization.organization_id == api_key.tenant_id,
                        Organization.status == "active",
                    )
                    .first()
                )
            else:
                organization = (
                    db.query(Organization)
                    .filter(
                        Organization.domain == request_domain,
                        Organization.status == "active",
                    )
                    .first()
                )

            if not organization:
                return _unauthorized("Organization not found or inactive")

            request.state.tenant_id = organization.organization_id
            request.state.tenant = organization

        finally:
            db.close()

        return await call_next(request)


_ORG_HOSTNAME_TTL_SECONDS = 60.0
_org_hostname_lock = threading.Lock()
_org_hostnames: set[str] = set()
_org_hostnames_at: Optional[float] = None


def _hostname_from_origin_or_domain(value: str) -> Optional[str]:
    """Normalize `https://gym.example.com` or `gym.example.com` to a hostname."""
    raw = str(value or "").strip().rstrip("/").lower()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    host = urlparse(raw).hostname
    return host.lower() if host else None


def _load_active_org_hostnames() -> set[str]:
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(Organization.domain)
            .filter(Organization.status == "active")
            .all()
        )
        hostnames: set[str] = set()
        for (domain,) in rows:
            host = _hostname_from_origin_or_domain(domain)
            if host:
                hostnames.add(host)
        return hostnames
    finally:
        db.close()


def _active_org_hostnames() -> set[str]:
    """Cached hostnames from master `organizations.domain` (active rows only)."""
    global _org_hostnames, _org_hostnames_at
    now = time.monotonic()
    if _org_hostnames_at is not None and (now - _org_hostnames_at) < _ORG_HOSTNAME_TTL_SECONDS:
        return _org_hostnames
    with _org_hostname_lock:
        now = time.monotonic()
        if _org_hostnames_at is not None and (now - _org_hostnames_at) < _ORG_HOSTNAME_TTL_SECONDS:
            return _org_hostnames
        try:
            _org_hostnames = _load_active_org_hostnames()
            _org_hostnames_at = now
        except Exception:
            logger.exception("Failed to load organization domains for CORS")
            if _org_hostnames_at is not None:
                return _org_hostnames
            return set()
        return _org_hostnames


def origin_allowed_by_org_domain(origin: str) -> bool:
    host = _hostname_from_origin_or_domain(origin)
    if not host:
        return False
    return host in _active_org_hostnames()


class DynamicCORSMiddleware(FastAPICORSMiddleware):
    """
    Allow origins from `BACKEND_CORS_ORIGINS` plus any active Organization.domain
    in the master DB (custom tenant sites).
    """

    def is_allowed_origin(self, origin: str) -> bool:
        if super().is_allowed_origin(origin):
            return True
        return origin_allowed_by_org_domain(origin)
