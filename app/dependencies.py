from typing import Generator, Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.core.db.session import SessionLocal
from app.core.security import verify_token
from app.models.user import User
from app.models.tenant import Tenant
from uuid import UUID

bearer_scheme = HTTPBearer(
    scheme_name="BearerAuth",
    description="Enter your JWT access token",
    auto_error=True
)


def get_db() -> Generator:
    """
    Database dependency that yields a database session.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db)
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
    
    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    Get current active user.
    """
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


async def get_current_tenant_id(
    request: Request,
) -> UUID:
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
) -> Tenant:
    """
    Get full tenant object from request state (set by TenantMiddleware).
    """
    if hasattr(request.state, "tenant"):
        return request.state.tenant
    
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="X-Tenant-Key header missing or invalid",
    )
