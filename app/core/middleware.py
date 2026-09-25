from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional, Union
from urllib.parse import urlparse

from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware as FastAPICORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.db.master_db import SessionLocal
from app.core.redis.cache import cache, tenant_key
from app.core.security import verify_token
from app.core.settings import settings
from app.models.master_org import Organization
from app.models.master_org_apikey import APIKeyStatus, OrganizationAPIKey

logger = logging.getLogger(__name__)

_api_prefix = settings.API_V1_STR.rstrip("/")

EXCLUDED_PATHS = {
    "/health",
    f"{_api_prefix}/docs",
    f"{_api_prefix}/redoc",
    f"{_api_prefix}/openapi.json",
}
_DOCS_PREFIXES = (f"{_api_prefix}/docs", f"{_api_prefix}/redoc")
_PAYMENT_REDIRECT_PATHS = {
    f"{_api_prefix}/payment/success",
    f"{_api_prefix}/payment/cancel",
}
_PAYMENT_CALLBACK_PREFIXES = (
    f"{_api_prefix}/payment/callback/",
    f"{_api_prefix}/callback/",
)

ResolveResult = Union[Organization, JSONResponse]


def _nonempty(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _host_from_url(raw: Optional[str], *, keep_port: bool) -> Optional[str]:
    """Parse Origin/Referer-style URLs. Hub matching keeps the port (localhost:3001)."""
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.hostname:
        return None
    host = parsed.netloc.split("@")[-1] if keep_port else parsed.hostname
    return host.lower() or None


def _hostname_from_origin_or_domain(value: str) -> Optional[str]:
    """Normalize `https://gym.example.com` or `gym.example.com` to a hostname (no port)."""
    raw = str(value or "").strip().rstrip("/").lower()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    host = urlparse(raw).hostname
    return host.lower() if host else None


def _origin_or_referer_host(request: Request) -> Optional[str]:
    for header in ("origin", "referer"):
        host = _host_from_url(request.headers.get(header), keep_port=True)
        if host:
            return host
    return None


def _extract_request_domain(request: Request) -> Optional[str]:
    """
    Calling site's host, port kept so `localhost:3000` and `:3001` are distinct.

    Origin/Referer first (browser). Host / X-Forwarded-Host only as a fallback
    for non-browser callers — those headers are the API host, not the tenant site.
    """
    host = _origin_or_referer_host(request)
    if host:
        return host
    raw = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not raw:
        return None
    host = raw.split(",")[0].strip()
    return host.lower() or None


def _is_hub_hostname(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    allowed = {h.strip().lower() for h in settings.TENANT_HUB_HOSTNAMES if h.strip()}
    return hostname.lower() in allowed


def _hub_tenant_id(request: Request) -> Optional[str]:
    return _nonempty(
        request.headers.get("X-Tenant-Id") or request.query_params.get("tenant_id")
    )


def _public_qr_tenant_id(request: Request) -> Optional[str]:
    path = request.url.path
    if not path.endswith("/qr") or "/bookings/" not in path:
        return None
    token = request.query_params.get("token")
    if token:
        payload = verify_token(token)
        if payload:
            tid = _nonempty(payload.get("tenant_id"))
            if tid:
                return tid
    return _nonempty(
        request.query_params.get("tenant_id") or request.headers.get("X-Tenant-Id")
    )


def _skip_tenant_resolution(request: Request) -> bool:
    if request.method == "OPTIONS":
        return True
    path = request.url.path
    if path in EXCLUDED_PATHS or path.startswith(_DOCS_PREFIXES):
        return True
    if path in _PAYMENT_REDIRECT_PATHS or path.startswith(_PAYMENT_CALLBACK_PREFIXES):
        return True
    return not path.startswith("/api/")


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"success": False, "message": message, "detail": message},
    )


def _attach_tenant(request: Request, organization: Organization) -> None:
    request.state.tenant_id = organization.organization_id
    request.state.tenant = organization


@contextmanager
def _master_session() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _lookup_active_organization(
    db: Session,
    *,
    organization_id: Optional[str] = None,
    domain: Optional[str] = None,
) -> Optional[Organization]:
    query = db.query(Organization).filter(Organization.status == "active")
    if organization_id:
        return query.filter(
            func.lower(Organization.organization_id) == organization_id.strip().lower()
        ).first()
    if domain:
        return query.filter(Organization.domain == domain).first()
    return None


def _found_org(organization: Optional[Organization]) -> ResolveResult:
    if organization is None:
        return _unauthorized("Organization not found or inactive")
    return organization


def domain_key(domain: str) -> str:
    """Hostname -> organization id, as written by the auth service."""
    return f"domain:{domain.strip().lower()}"


def org_cache_key(organization_id: str) -> str:
    """Organization payload, e.g. ``t:ORG-110``. The id keeps its case."""
    return tenant_key(organization_id)


def _org_from_cache(request_domain: str) -> Optional[Organization]:
    """
    Resolve from auth-service Redis entries:
    ``domain:<host> -> ORG-110`` then ``t:ORG-110 -> {...}``.
    """
    org_id = cache.get_text(domain_key(request_domain))
    if not org_id:
        return None

    payload = cache.get(org_cache_key(org_id))
    if not isinstance(payload, dict):
        return None

    # Absent status means the auth service only caches active orgs.
    if str(payload.get("status") or "active").lower() != "active":
        return None

    return Organization(
        organization_id=payload.get("organization_id") or org_id,
        name=payload.get("name"),
        domain=payload.get("domain") or request_domain,
        status="active",
    )


def _cache_org(request_domain: str, organization: Organization) -> None:
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


def _resolve_by_organization_id(organization_id: str) -> ResolveResult:
    with _master_session() as db:
        return _found_org(
            _lookup_active_organization(db, organization_id=organization_id)
        )


def _resolve_by_api_key(api_key_value: str) -> ResolveResult:
    with _master_session() as db:
        api_key = (
            db.query(OrganizationAPIKey)
            .filter(
                OrganizationAPIKey.api_key == api_key_value,
                OrganizationAPIKey.status == APIKeyStatus.active,
            )
            .first()
        )
        if not api_key:
            return _unauthorized("Invalid or inactive organization API key")
        return _found_org(
            _lookup_active_organization(db, organization_id=api_key.tenant_id)
        )


def _resolve_by_domain(request_domain: str) -> ResolveResult:
    cached = _org_from_cache(request_domain)
    if cached is not None:
        return cached
    with _master_session() as db:
        organization = _lookup_active_organization(db, domain=request_domain)
        if organization is None:
            return _unauthorized("Organization not found or inactive")
        _cache_org(request_domain, organization)
        return organization


def _resolve_tenant(request: Request) -> ResolveResult:
    """
    Resolution order:
      1. Hub origin + tenant_id / X-Tenant-Id (wins over a stale API key)
      2. Public booking QR (`/bookings/.../qr`)
      3. X-Tenant-Key
      4. Tenant site domain (Origin / Referer, then Host)
    """
    api_key = request.headers.get("X-Tenant-Key")
    origin_hostname = _origin_or_referer_host(request)
    request_domain = _extract_request_domain(request)
    is_hub = _is_hub_hostname(origin_hostname)
    hub_tenant_id = _hub_tenant_id(request) if is_hub else None
    qr_tenant_id = _public_qr_tenant_id(request)

    logger.debug(
        "Resolving tenant: domain=%s origin=%s hub=%s api_key=%s hub_tenant_id=%s public_qr_tenant_id=%s",
        request_domain,
        origin_hostname,
        is_hub,
        "present" if api_key else "absent",
        hub_tenant_id or "(none)",
        qr_tenant_id or "(none)",
    )

    if is_hub and hub_tenant_id:
        return _resolve_by_organization_id(hub_tenant_id)
    if qr_tenant_id:
        return _resolve_by_organization_id(qr_tenant_id)
    if api_key:
        return _resolve_by_api_key(api_key)
    if is_hub:
        return _unauthorized(
            "tenant_id query parameter or X-Tenant-Id header is required for hub requests"
        )
    if not request_domain:
        return _unauthorized(
            "Either X-Tenant-Key header or a request domain is required"
        )
    return _resolve_by_domain(request_domain)


class TenantMiddleware(BaseHTTPMiddleware):
    """Attach `request.state.tenant_id` / `request.state.tenant` for /api/* requests."""

    async def dispatch(self, request: Request, call_next):
        if _skip_tenant_resolution(request):
            return await call_next(request)

        result = _resolve_tenant(request)
        if isinstance(result, JSONResponse):
            return result

        _attach_tenant(request, result)
        return await call_next(request)


_ORG_HOSTNAME_TTL_SECONDS = 60.0
_org_hostname_lock = threading.Lock()
_org_hostnames: set[str] = set()
_org_hostnames_at: Optional[float] = None


def _org_hostname_cache_fresh(now: float) -> bool:
    return (
        _org_hostnames_at is not None
        and (now - _org_hostnames_at) < _ORG_HOSTNAME_TTL_SECONDS
    )


def _load_active_org_hostnames() -> set[str]:
    with _master_session() as db:
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


def _active_org_hostnames() -> set[str]:
    """Cached hostnames from master `organizations.domain` (active rows only)."""
    global _org_hostnames, _org_hostnames_at
    now = time.monotonic()
    if _org_hostname_cache_fresh(now):
        return _org_hostnames
    with _org_hostname_lock:
        now = time.monotonic()
        if _org_hostname_cache_fresh(now):
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
    host = _host_from_url(origin, keep_port=True) or _hostname_from_origin_or_domain(
        origin
    )
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
