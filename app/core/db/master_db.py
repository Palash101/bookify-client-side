from __future__ import annotations

import logging
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.settings import settings

logger = logging.getLogger(__name__)


master_engine: Engine = create_engine(
    settings.database_url,
    poolclass=QueuePool,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1_800,
    pool_pre_ping=True,
)


SessionLocal: sessionmaker = sessionmaker(
    bind=master_engine,
    expire_on_commit=False,
    autoflush=False,
)


def get_master_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a session against the master/control-plane DB
    (tenants, tenant API keys, and other shared metadata).
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        try:
            db.rollback()
        except Exception:
            logger.exception("Failed to rollback master DB session")
        raise
    finally:
        db.close()
