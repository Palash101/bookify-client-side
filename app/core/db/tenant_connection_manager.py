from __future__ import annotations

import json
import logging
from threading import Lock
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool

from app.core.settings import get_settings
from app.core.secret_manager import get_tenant_db_secret

logger = logging.getLogger(__name__)


class SingletonMeta(type):
    """Metaclass implementing the Singleton pattern in a thread-safe manner."""

    _instances: Dict[type, "TenantDBConnectionManager"] = {}
    _instances_lock: Lock = Lock()

    def __call__(cls, *args: Any, **kwargs: Any) -> "TenantDBConnectionManager":
        with cls._instances_lock:
            if cls not in cls._instances:
                instance = super().__call__(*args, **kwargs)
                cls._instances[cls] = instance
        return cls._instances[cls]


class TenantDBConnectionManager(metaclass=SingletonMeta):
    """
    Manages tenant-specific PostgreSQL engines backed by a connection pool.

    The manager retrieves database credentials from GCP Secret Manager on demand,
    constructs a SQLAlchemy engine with pooling, and caches the engine for reuse
    across subsequent requests. Engines are created lazily and only once per tenant.
    """

    def __init__(
        self,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: int = 30,
        pool_recycle: int = 1_800,
        connect_args: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._connect_args = connect_args or {}
        self._pool_kwargs = {
            "poolclass": QueuePool,
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "pool_timeout": pool_timeout,
            "pool_recycle": pool_recycle,
            "pool_pre_ping": True,
        }
        self._engines: Dict[str, Engine] = {}
        self._lock = Lock()

    def get_engine(self, tenant: str) -> Engine:
        """
        Return a pooled SQLAlchemy engine for the given tenant.

        The engine is created on first access using credentials loaded from
        GCP Secret Manager and cached for subsequent requests.
        """
        if not tenant:
            raise ValueError("tenant must be a non-empty string")

        try:
            return self._engines[tenant]
        except KeyError:
            pass

        with self._lock:
            engine = self._engines.get(tenant)
            if engine is not None:
                return engine

            credentials = self._get_credentials_for_tenant(tenant)
            engine = self._create_engine(credentials)
            self._engines[tenant] = engine
            return engine

    def dispose_engine(self, tenant: str) -> None:
        """Dispose and remove the cached engine for a tenant, if present."""
        with self._lock:
            engine = self._engines.pop(tenant, None)

        if engine is not None:
            engine.dispose()

    def clear(self) -> None:
        """Dispose every cached engine and clear the cache."""
        with self._lock:
            engines = list(self._engines.items())
            self._engines.clear()

        for tenant, engine in engines:
            logger.debug("Disposing engine for tenant %s", tenant)
            engine.dispose()

    def get_pool_statistics(self) -> Dict[str, Dict[str, int]]:
        """
        Return QueuePool counters for each tenant engine currently in cache.

        Per SQLAlchemy's QueuePool: ``pool_size_limit`` matches configured pool_size
        (max queue capacity), ``idle_in_pool`` is connections idle in the pool queue,
        ``checked_out`` is connections handed to the application, ``overflow_connections``
        is the pool's overflow count (extra connections beyond pool_size).
        """
        with self._lock:
            items = tuple(self._engines.items())

        stats: Dict[str, Dict[str, int]] = {}
        for tenant, engine in items:
            pool = engine.pool
            if hasattr(pool, "checkedout"):
                stats[tenant] = {
                    "pool_size_limit": int(pool.size()),
                    "idle_in_pool": int(pool.checkedin()),
                    "checked_out": int(pool.checkedout()),
                    "overflow_connections": int(pool.overflow()),
                }
            else:
                stats[tenant] = {
                    "pool_size_limit": 0,
                    "idle_in_pool": 0,
                    "checked_out": 0,
                    "overflow_connections": 0,
                }
        return stats

    def _get_credentials_for_tenant(self, tenant: str) -> Dict[str, Any]:
        """
        Get database credentials for a tenant.
        In local development mode, uses environment variables.
        In production mode, loads the tenant secret via app.core.secret_manager.
        """
        settings = get_settings()
        
        # Check if we're in local development mode
        is_local = settings.MODE.lower() in ("development", "local", "dev")
        
        # if is_local:
        #     logger.info("Using local development database credentials for tenant: %s", tenant)
        #     # Use local PostgreSQL credentials from environment variables
        #     return {
        #         "username": settings.LOCAL_POSTGRES_USER,
        #         "password": settings.LOCAL_POSTGRES_PASSWORD,
        #         "host": settings.LOCAL_POSTGRES_HOST,
        #         "port": settings.LOCAL_POSTGRES_PORT,
        #         "database": settings.LOCAL_POSTGRES_DB,
        #     }

        secret_payload_str = get_tenant_db_secret(tenant)
        if not secret_payload_str:
            raise RuntimeError(f"Tenant DB secret not configured for tenant '{tenant}'")

        try:
            secret_payload = json.loads(secret_payload_str)
            
            
            payload = {
                "username": secret_payload.get("user"),
                "password": secret_payload.get("password"),
                "host": secret_payload.get("host"),
                "port": "5432",
                "database": secret_payload.get("database"),
            }
            
        except json.JSONDecodeError as exc:
            logger.exception(
                "Unable to retrieve database credentials for tenant %s", tenant
            )
            raise RuntimeError(
                f"Unable to retrieve database credentials for tenant '{tenant}': {str(exc)}"
            ) from exc

        return payload

    def _create_engine(self, credentials: Dict[str, Any]) -> Engine:
        url = self._build_connection_url(credentials)

        logger.debug("Creating PostgreSQL engine for %s", credentials["host"])

        return create_engine(url, connect_args=self._connect_args, **self._pool_kwargs)

    @staticmethod
    def _build_connection_url(credentials: Dict[str, Any]) -> str:
        username = quote_plus(credentials["username"])
        password = quote_plus(credentials["password"])
        host = credentials["host"]
        port = credentials.get("port", 5432)
        database = credentials["database"]

        options = credentials.get("options")
        options_fragment = f"?{options}" if options else ""

        return (
            f"postgresql+psycopg2://{username}:{password}@{host}:{port}/{database}"
            f"{options_fragment}"
        )


def get_tenant_connection_manager() -> TenantDBConnectionManager:
    """
    Convenience accessor that wires the manager with application settings.
    """
    settings = get_settings()

    return TenantDBConnectionManager(
        pool_size=int(getattr(settings, "POSTGRES_POOL_SIZE", 5)),
        max_overflow=int(getattr(settings, "POSTGRES_MAX_OVERFLOW", 10)),
        pool_timeout=int(getattr(settings, "POSTGRES_POOL_TIMEOUT", 30)),
        pool_recycle=int(getattr(settings, "POSTGRES_POOL_RECYCLE", 1_800)),
        connect_args=getattr(settings, "POSTGRES_CONNECT_ARGS", None),
    )

