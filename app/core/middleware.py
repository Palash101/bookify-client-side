from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware as FastAPICORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.core.db.master_db import SessionLocal
from app.models.master_org import Organization
from app.models.master_org_apikey import APIKeyStatus, OrganizationAPIKey
from app.core.redis.cache import cache, tenant_key
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


def _hostname_from_origin_header(raw: Optional[str]) -> Optional[str]:
    """
    Host used for hub matching. Keeps port when present so local hubs like
    ``localhost:3001`` are distinct from ``localhost:3000``.
    """
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.hostname:
        return None
    host = parsed.netloc.split("@")[-1].lower()
    return host or None


def _extract_origin_hostname(request: Request) -> Optional[str]:
    """Browser origin only (Origin / Referer). Used for hub-site tenant routing."""
    for header in ("origin", "referer"):
        host = _hostname_from_origin_header(request.headers.get(header))
        if host:
            return host
    return None


def _is_hub_hostname(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    allowed = {h.strip().lower() for h in settings.TENANT_HUB_HOSTNAMES if h.strip()}
    return hostname.lower() in allowed


def _hub_tenant_id(request: Request) -> Optional[str]:
    raw = request.headers.get("X-Tenant-Id") or request.query_params.get("tenant_id")
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _lookup_active_organization(
    db: Session,
    *,
    organization_id: Optional[str] = None,
    domain: Optional[str] = None,
) -> Optional[Organization]:
    query = db.query(Organization).filter(Organization.status == "active")
    if organization_id:
        return (
            query.filter(
                func.lower(Organization.organization_id) == organization_id.strip().lower()
            ).first()
        )
    if domain:
        return query.filter(Organization.domain == domain).first()
    return None


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

    The port is preserved so distinct local origins such as `localhost:3000`
    and `localhost:3001` resolve to different tenants and match the value
    stored on `Organization.domain`.
    """
    # Preferred: the frontend origin reported by the browser.
    for header in ("origin", "referer"):
        raw = request.headers.get(header)
        if not raw:
            continue
        parsed = urlparse(raw)
        if parsed.hostname:
            # netloc keeps the port (and strips any userinfo); lowercase for
            # case-insensitive host matching (ports are digits, unaffected).
            host = parsed.netloc.split("@")[-1]
            return host.lower()

    # Fallback for non-browser callers that don't send Origin/Referer.
    raw = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not raw:
        return None
    # X-Forwarded-Host may contain a comma-separated list; take the first entry.
    host = raw.split(",")[0].strip()
    if not host:
        return None
    return host.lower() or None


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"success": False, "message": message, "detail": message},
    )


def domain_key(domain: str) -> str:
    """Hostname -> organization id, as written by the auth service."""
    return f"domain:{domain.strip().lower()}"


def org_cache_key(organization_id: str) -> str:
    """Organization payload, e.g. ``t:ORG-110``. The id keeps its case."""
    return tenant_key(organization_id)


def _org_from_cache(request_domain: str) -> Optional[Organization]:
    """
    Resolve an organization from the entries the auth service writes:
    ``domain:<host> -> ORG-110`` then ``t:ORG-110 -> {...}``.

    Returns a transient (session-less) Organization carrying just the fields
    downstream code reads, or None to fall through to the master DB.
    """
    org_id = cache.get_text(domain_key(request_domain))
    if not org_id:
        return None

    payload = cache.get(org_cache_key(org_id))
    if not isinstance(payload, dict):
        return None

    # Absent status means the auth service only caches active orgs; an explicit
    # non-active value is honoured so a blocked org cannot slip through.
    if str(payload.get("status") or "active").lower() != "active":
        return None

    return Organization(
        organization_id=payload.get("organization_id") or org_id,
        name=payload.get("name"),
        domain=payload.get("domain") or request_domain,
        status="active",
    )


def _cache_org(request_domain: str, organization: Organization) -> None:
    """Write the same two entries the auth service does, after a DB lookup."""
    cache.set(domain_key(request_domain), organization.organization_id)
    cache.set(
        org_cache_key(organization.organization_id),
        {
            "organization_id": organization.organization_id,
            "name": organization.name,
            "domain": organization.domain,
            "status": organization.status,
        },
    )


class TenantMiddleware(BaseHTTPMiddleware):
    """
    Middleware that resolves the active organization for every API request.

    Resolution order:
      1. `X-Tenant-Key` — active API key → organization (any caller).
      2. Hub origin (`localhost:3001` locally; later `booking.fitnezstudios.com`) —
         `tenant_id` query param or `X-Tenant-Id` header required → organization by id.
      3. Tenant site domain — `Origin` / `Referer` matches `Organization.domain`.

    On success, `request.state.tenant_id` and `request.state.tenant` are set.
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
        origin_hostname = _extract_origin_hostname(request)
        request_domain = _extract_request_domain(request)
        hub_request = _is_hub_hostname(origin_hostname)
        hub_tenant_id = _hub_tenant_id(request) if hub_request else None
        # Never log the key itself -- it is a credential. Whether one was sent
        # is all that is needed to debug tenant resolution.
        logger.debug(
            "Resolving tenant: domain=%s origin=%s hub=%s api_key=%s hub_tenant_id=%s",
            request_domain,
            origin_hostname,
            hub_request,
            "present" if x_tenant_key else "absent",
            hub_tenant_id or "(none)",
        )

        if not x_tenant_key and not hub_request and not request_domain:
            return _unauthorized(
                "Either X-Tenant-Key header or a request domain is required"
            )

        if hub_request and not x_tenant_key and not hub_tenant_id:
            return _unauthorized(
                "tenant_id query parameter or X-Tenant-Id header is required for hub requests"
            )

        # Domain-only tenant sites can be served entirely from Redis.
        if not x_tenant_key and not hub_request:
            cached_org = _org_from_cache(request_domain)
            if cached_org is not None:
                request.state.tenant_id = cached_org.organization_id
                request.state.tenant = cached_org
                return await call_next(request)

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

                organization = _lookup_active_organization(
                    db, organization_id=api_key.tenant_id
                )
            elif hub_request:
                organization = _lookup_active_organization(
                    db, organization_id=hub_tenant_id
                )
            else:
                organization = _lookup_active_organization(db, domain=request_domain)

            if not organization:
                return _unauthorized("Organization not found or inactive")

            if not x_tenant_key and not hub_request:
                _cache_org(request_domain, organization)

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


def origin_allowed_by_hub(origin: str) -> bool:
    # Prefer host:port (netloc) so local hubs like localhost:3001 match.
    host = _hostname_from_origin_header(origin)
    if not host:
        host = _hostname_from_origin_or_domain(origin)
    if not host:
        return False
    return _is_hub_hostname(host)


class DynamicCORSMiddleware(FastAPICORSMiddleware):
    """
    Allow origins from `BACKEND_CORS_ORIGINS` plus any active Organization.domain
    in the master DB (custom tenant sites).
    """

    def is_allowed_origin(self, origin: str) -> bool:
        if super().is_allowed_origin(origin):
            return True
        if origin_allowed_by_hub(origin):
            return True
        return origin_allowed_by_org_domain(origin)
