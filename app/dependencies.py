from typing import Generator, Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.core.db.session import get_db as tenant_get_db
from app.core.security import verify_token
from app.models.user import User
from app.models.master_org import Organization
from app.schemas.gym_config_value import GymConfigValue
from app.services.gym_config_service import GymConfigService
from uuid import UUID

bearer_scheme = HTTPBearer(
    scheme_name="BearerAuth",
    description="Enter your JWT access token",
    auto_error=True
)

optional_bearer_scheme = HTTPBearer(
    scheme_name="BearerAuthOptional",
    description="Optional JWT access token",
    auto_error=False,
)


def get_db(request: Request) -> Generator:
    """
    Tenant-scoped database dependency.

    Uses `TenantMiddleware` -> `request.state.tenant_id` to connect to the correct tenant DB.
    """
    yield from tenant_get_db(request)


def _request_tenant_id(request: Request) -> Optional[str]:
    raw = getattr(request.state, "tenant_id", None)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _tenant_ids_match(token_or_user_tenant: object, request_tenant: Optional[str]) -> bool:
    """True when request has no tenant yet, or both sides equal (case-insensitive)."""
    if request_tenant is None:
        return True
    if token_or_user_tenant is None:
        return False
    return str(token_or_user_tenant).strip().lower() == request_tenant.strip().lower()


def _raise_tenant_mismatch() -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token tenant does not match request tenant_id. Log in again for this organization.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    Get current authenticated user from token.

    JWT / user tenant must match the request tenant resolved by TenantMiddleware
    (hub tenant_id, API key, or domain).
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    token = credentials.credentials
    payload = verify_token(token)
    if payload is None:
        raise credentials_exception

    user_id_str: str = payload.get("sub")
    if user_id_str is None:
        raise credentials_exception

    try:
        user_id = UUID(user_id_str)
    except (ValueError, TypeError):
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception

    # Session gym must match JWT (same email can exist on multiple tenants)
    tid_claim = payload.get("tenant_id")
    if tid_claim is not None:
        if str(tid_claim) != str(user.tenant_id):
            raise credentials_exception

    request_tenant = _request_tenant_id(request)
    token_tenant = tid_claim if tid_claim is not None else user.tenant_id
    if not _tenant_ids_match(token_tenant, request_tenant):
        _raise_tenant_mismatch()
    if not _tenant_ids_match(user.tenant_id, request_tenant):
        _raise_tenant_mismatch()

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    Get current active user.
    """
    # Legacy rows may have NULL; treat NULL as active.
    if current_user.is_active is False:
        raise HTTPException(
            status_code=400,
            detail="Your account is inactive. Please contact support to reactivate your account.",
        )
    return current_user


async def get_optional_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    Return the logged-in user when a valid Bearer token is sent for this request's tenant.

    Wrong-org tokens are ignored (treated as logged out) so public pages still load.
    """
    if credentials is None:
        return None

    token = credentials.credentials
    payload = verify_token(token)
    if payload is None:
        return None

    user_id_str = payload.get("sub")
    if user_id_str is None:
        return None

    try:
        user_id = UUID(user_id_str)
    except (ValueError, TypeError):
        return None

    user = db.query(User).filter(User.id == user_id).first()
    if user is None or user.is_active is False:
        return None

    tid_claim = payload.get("tenant_id")
    if tid_claim is not None and str(tid_claim) != str(user.tenant_id):
        return None

    request_tenant = _request_tenant_id(request)
    token_tenant = tid_claim if tid_claim is not None else user.tenant_id
    if not _tenant_ids_match(token_tenant, request_tenant):
        return None
    if not _tenant_ids_match(user.tenant_id, request_tenant):
        return None

    return user


async def get_gym_config_for_active_user(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> GymConfigValue:
    """
    Reads TenantSetting gym_config once per request.
    Share this dependency on endpoints that also use get_current_active_user — FastAPI caches the user dependency.
    """
    return GymConfigService.get_gym_config(db, current_user.tenant_id)


async def get_current_tenant_id(
    request: Request,
) -> str:
    """
    Get tenant_id from request state (set by TenantMiddleware).
    """
    if hasattr(request.state, "tenant_id"):
        return request.state.tenant_id

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="X-Tenant-Key header missing or invalid",
    )


async def get_current_tenant(
    request: Request,
) -> Organization:
    """
    Resolve current tenant from request state (set by TenantMiddleware).

    TenantMiddleware attaches `request.state.tenant_id` based on the X-Tenant-Key (or domain)
    via the master/control-plane DB. Here we use that id to load the actual `tenants` row
    from the application DB.
    """
    # For client APIs we resolve tenant through TenantMiddleware which uses the
    # master/control-plane DB and attaches the Organization object directly.
    if hasattr(request.state, "tenant"):
        return request.state.tenant

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="X-Tenant-Key header missing or invalid",
    )
