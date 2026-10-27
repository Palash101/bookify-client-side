from __future__ import annotations

import logging
from threading import Lock
from typing import AsyncIterator, Dict, Optional

from fastapi import Request
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.db.tenant_connection_manager import get_tenant_connection_manager
from app.core.db.master_db import SessionLocal as _MasterSessionLocal

logger = logging.getLogger(__name__)

class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------------------
# Tenant-aware sessions for API requests
# --------------------------------------------------------------------------------------

_session_factory_cache: Dict[str, sessionmaker] = {}
_factory_lock: Lock = Lock()


def get_session_factory(tenant: str) -> sessionmaker:
    factory = _session_factory_cache.get(tenant)
    if factory is not None:
        return factory
    with _factory_lock:
        factory = _session_factory_cache.get(tenant)
        if factory is None:
            logger.debug("Creating session factory for tenant %s", tenant)
            factory = sessionmaker(
                bind=get_tenant_connection_manager().get_engine(tenant),
                expire_on_commit=False,
                autoflush=False,
            )
            _session_factory_cache[tenant] = factory
    return factory


def _get_tenant_from_request(request: Request) -> Optional[str]:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id:
        return str(tenant_id).strip()
    return None


async def get_db(request: Request) -> AsyncIterator[Session]:
    """
    FastAPI dependency that returns a tenant-scoped SQLAlchemy Session.

    Existing endpoints can keep using: db: Session = Depends(get_db)

    Async on purpose: opening and closing the session then run on the event loop, not in
    the thread pool. As a sync generator, its cleanup (which hands the connection back)
    had to wait for a free thread; under load every thread was waiting for a connection,
    so finished requests never gave theirs back and the service deadlocked. A session
    opens no connection until its first query, and close() is a quick rollback.
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


# --------------------------------------------------------------------------------------
# Backwards-compatible export
# --------------------------------------------------------------------------------------
#
# Some parts of the codebase still import `SessionLocal` from `app.core.db.session`.
# Keep this alias so older modules continue to work while the project transitions to
# using `get_db` (tenant-scoped) or `app.core.db.master_db.SessionLocal` (master DB).
SessionLocal = _MasterSessionLocal

