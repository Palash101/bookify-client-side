from __future__ import annotations

import logging
from threading import Lock
from typing import Dict, Optional

from fastapi import Request
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.db.tenant_connection_manager import get_tenant_connection_manager

logger = logging.getLogger(__name__)

class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------------------
# Tenant-aware sessions for API requests
# --------------------------------------------------------------------------------------

_session_factory_cache: Dict[str, sessionmaker] = {}
_factory_lock: Lock = Lock()


def get_session_factory(tenant: str) -> sessionmaker:
    manager = get_tenant_connection_manager()
    engine_for_tenant = manager.get_engine(tenant)

    with _factory_lock:
        factory = _session_factory_cache.get(tenant)
        if factory is None:
            logger.debug("Creating session factory for tenant %s", tenant)
            factory = sessionmaker(
                bind=engine_for_tenant, expire_on_commit=False, autoflush=False
            )
            _session_factory_cache[tenant] = factory
    return factory


def _get_tenant_from_request(request: Request) -> Optional[str]:
    tenant_id = getattr(request.state, "tenant_id", None)
    print(tenant_id,'tenant_id m---')
    if tenant_id:
        return str(tenant_id).strip()
    return None


def get_db(request: Request) -> Session:
    """
    FastAPI dependency that returns a tenant-scoped SQLAlchemy Session.

    Existing endpoints can keep using: db: Session = Depends(get_db)
    """
    tenant = _get_tenant_from_request(request)
    if not tenant:
        raise RuntimeError("tenant_id not found on request; cannot create tenant DB session")

    factory = get_session_factory(tenant)

    db = factory()
    try:
        yield db
    except Exception:
        # Ensure we never leave an open transaction on errors.
        try:
            db.rollback()
        except Exception:
            logger.exception("Failed to rollback DB session")
        raise
    finally:
        db.close()

