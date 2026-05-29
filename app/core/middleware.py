from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.orm import Session
from app.core.db.master_db import SessionLocal
from app.models.master_org import Organization
from app.models.master_org_apikey import APIKeyStatus, OrganizationAPIKey
from app.core.settings import settings
import logging
from typing import Optional

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
    Resolve the originating domain of the request.

    Prefer the explicit `X-Forwarded-Host` header (set by upstream proxies / load balancers),
    falling back to the `Host` header. The port is stripped so lookups match the value
    stored on `Tenant.domain`.
    """
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

        # Webhook/callback endpoints are called by payment providers (no tenant header).
        if path.startswith(f"{_api_prefix}/payment/callback/"):
            return await call_next(request)

        if not path.startswith("/api/"):
            return await call_next(request)

        x_tenant_key = request.headers.get("X-Tenant-Key")
        print(x_tenant_key,'x_tenant_key')
        request_domain = _extract_request_domain(request)

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


class CORSMiddleware(BaseHTTPMiddleware):
    """
    Custom CORS middleware if needed.
    """
    
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response
