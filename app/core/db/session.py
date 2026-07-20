from __future__ import annotations

import logging
from threading import Lock
from typing import Dict, Generator, Optional

from fastapi import Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.db.tenant_connection_manager import get_tenant_connection_manager
from app.core.db.master_db import SessionLocal as _MasterSessionLocal
from app.core.settings import settings

logger = logging.getLogger(__name__)

class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------------------
# Tenant-aware sessions for API requests
# --------------------------------------------------------------------------------------

# Cache key: "{tenant}:write" or "{tenant}:read:{engine_id}"
_session_factory_cache: Dict[str, sessionmaker] = {}
_factory_lock: Lock = Lock()


def _engine_target(engine: Engine) -> str:
    """Human-readable host/db without credentials."""
    url = engine.url
    host = url.host or "?"
    port = url.port or "?"
    database = url.database or "?"
    return f"{host}:{port}/{database}"


def get_session_factory(tenant: str, *, read_only: bool = False) -> sessionmaker:
    manager = get_tenant_connection_manager()
    engine_for_tenant = manager.get_engine(tenant, read_only=read_only)
    cache_key = f"{tenant}:{'read' if read_only else 'write'}:{id(engine_for_tenant)}"

    with _factory_lock:
        factory = _session_factory_cache.get(cache_key)
        if factory is None:
            logger.debug(
                "Creating %s session factory for tenant %s -> %s",
                "read" if read_only else "write",
                tenant,
                _engine_target(engine_for_tenant),
            )
            factory = sessionmaker(
                bind=engine_for_tenant, expire_on_commit=False, autoflush=False
            )
            _session_factory_cache[cache_key] = factory
    return factory


def _get_tenant_from_request(request: Request) -> Optional[str]:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id:
        return str(tenant_id).strip()
    return None


def get_db(request: Request, *, read_only: bool = False) -> Generator[Session, None, None]:
    """
    FastAPI dependency that returns a tenant-scoped SQLAlchemy Session.

    Prefer ``get_read_db`` (GET) / ``get_write_db`` (POST/PUT/PATCH/DELETE).
    """
    tenant = _get_tenant_from_request(request)
    if not tenant:
        raise RuntimeError("tenant_id not found on request; cannot create tenant DB session")

    role = "read" if read_only else "write"
    factory = get_session_factory(tenant, read_only=read_only)

    db = factory()
    if settings.DEBUG:
        bind = db.get_bind()
        target = _engine_target(bind) if isinstance(bind, Engine) else str(getattr(bind, "url", bind))
        # print so it shows in uvicorn terminal (app loggers often aren't configured)
        print(
            f"[DB] role={role} tenant={tenant} target={target} "
            f"{request.method} {request.url.path}",
            flush=True,
        )
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


def get_read_db(request: Request) -> Generator[Session, None, None]:
    """Read replica session — use for GET APIs."""
    yield from get_db(request, read_only=True)


def get_write_db(request: Request) -> Generator[Session, None, None]:
    """Primary/write session — use for POST/PUT/PATCH/DELETE APIs."""
    yield from get_db(request, read_only=False)


# --------------------------------------------------------------------------------------
# Backwards-compatible export
# --------------------------------------------------------------------------------------
#
# Some parts of the codebase still import `SessionLocal` from `app.core.db.session`.
# Keep this alias so older modules continue to work while the project transitions to
# using `get_db` (tenant-scoped) or `app.core.db.master_db.SessionLocal` (master DB).
SessionLocal = _MasterSessionLocal
