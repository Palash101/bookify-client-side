from typing import Generator, Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.core.db.session import (
    get_db as tenant_get_db,
    get_read_db as tenant_get_read_db,
    get_write_db as tenant_get_write_db,
)
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
    Tenant-scoped write DB dependency (primary). Prefer get_read_db / get_write_db.
    """
    yield from tenant_get_db(request, read_only=False)


def get_read_db(request: Request) -> Generator:
    """Tenant-scoped read DB — use for GET APIs."""
    yield from tenant_get_read_db(request)


def get_write_db(request: Request) -> Generator:
    """Tenant-scoped write DB — use for POST/PUT/PATCH/DELETE APIs."""
    yield from tenant_get_write_db(request)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_read_db)
) -> User:
    """
    Get current authenticated user from token.
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
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer_scheme),
    db: Session = Depends(get_read_db),
) -> Optional[User]:
    """Return the logged-in user when a valid Bearer token is sent; otherwise None."""
    if credentials is None:
        return None

    token = credentials.credentials
    payload = verify_token(token)
    if payload is None:
        return None

    user_id_str: str = payload.get("sub")
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

    return user


async def get_gym_config_for_active_user(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_read_db),
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
