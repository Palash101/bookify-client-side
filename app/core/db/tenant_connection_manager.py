from __future__ import annotations

import json
import logging
from threading import Lock, RLock
from typing import Any, Dict, List, Optional, Sequence
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

    Write: one primary engine per tenant (single URL).
    Read: one or more replica engines per tenant; requests round-robin across them.
    If no read hosts are configured, reads fall back to the write engine.
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
        self._write_engines: Dict[str, Engine] = {}
        self._read_engines: Dict[str, List[Engine]] = {}
        self._read_rr_index: Dict[str, int] = {}
        # RLock: _get_read_engine may call _next_read_engine while already holding the lock.
        self._lock = RLock()

    def get_engine(self, tenant: str, *, read_only: bool = False) -> Engine:
        """
        Return a pooled SQLAlchemy engine for the given tenant.

        ``read_only=True`` selects a read replica (round-robin when multiple exist).
        ``read_only=False`` always returns the single write/primary engine.
        """
        if not tenant:
            raise ValueError("tenant must be a non-empty string")

        if read_only:
            return self._get_read_engine(tenant)
        return self._get_write_engine(tenant)

    def _get_write_engine(self, tenant: str) -> Engine:
        try:
            return self._write_engines[tenant]
        except KeyError:
            pass

        with self._lock:
            engine = self._write_engines.get(tenant)
            if engine is not None:
                return engine

            self._ensure_engines_locked(tenant)
            return self._write_engines[tenant]

    def _get_read_engine(self, tenant: str) -> Engine:
        engines = self._read_engines.get(tenant)
        if engines:
            return self._next_read_engine(tenant, engines)

        with self._lock:
            engines = self._read_engines.get(tenant)
            if not engines:
                self._ensure_engines_locked(tenant)
                engines = self._read_engines[tenant]
            return self._next_read_engine(tenant, engines)

    def _next_read_engine(self, tenant: str, engines: Sequence[Engine]) -> Engine:
        if len(engines) == 1:
            return engines[0]

        with self._lock:
            index = self._read_rr_index.get(tenant, 0)
            engine = engines[index % len(engines)]
            self._read_rr_index[tenant] = (index + 1) % len(engines)
            return engine

    def _ensure_engines_locked(self, tenant: str) -> None:
        """Create and cache write + read engines. Caller must hold ``self._lock``."""
        if tenant in self._write_engines:
            return

        credentials = self._get_credentials_for_tenant(tenant)
        write_engine = self._create_engine(credentials["write"])
        self._write_engines[tenant] = write_engine

        read_creds_list = credentials["read"]
        if not read_creds_list:
            self._read_engines[tenant] = [write_engine]
        else:
            self._read_engines[tenant] = [
                self._create_engine(creds) for creds in read_creds_list
            ]
        self._read_rr_index[tenant] = 0

    def dispose_engine(self, tenant: str) -> None:
        """Dispose and remove the cached engines for a tenant, if present."""
        with self._lock:
            write_engine = self._write_engines.pop(tenant, None)
            read_engines = self._read_engines.pop(tenant, None) or []
            self._read_rr_index.pop(tenant, None)

        seen: set[int] = set()
        for engine in [write_engine, *read_engines]:
            if engine is None:
                continue
            engine_id = id(engine)
            if engine_id in seen:
                continue
            seen.add(engine_id)
            engine.dispose()

    def clear(self) -> None:
        """Dispose every cached engine and clear the cache."""
        with self._lock:
            write_items = list(self._write_engines.items())
            read_items = list(self._read_engines.items())
            self._write_engines.clear()
            self._read_engines.clear()
            self._read_rr_index.clear()

        seen: set[int] = set()
        for _, engine in write_items:
            if id(engine) not in seen:
                seen.add(id(engine))
                logger.debug("Disposing write engine")
                engine.dispose()
        for tenant, engines in read_items:
            for engine in engines:
                if id(engine) in seen:
                    continue
                seen.add(id(engine))
                logger.debug("Disposing read engine for tenant %s", tenant)
                engine.dispose()

    def get_pool_statistics(self) -> Dict[str, Dict[str, int]]:
        """
        Return QueuePool counters for each tenant engine currently in cache.

        Keys are ``{tenant}:write`` or ``{tenant}:read:{i}``.
        """
        with self._lock:
            write_items = tuple(self._write_engines.items())
            read_items = tuple(
                (tenant, list(engines)) for tenant, engines in self._read_engines.items()
            )
            write_by_tenant = dict(write_items)

        stats: Dict[str, Dict[str, int]] = {}

        def _pool_stats(engine: Engine) -> Dict[str, int]:
            pool = engine.pool
            if hasattr(pool, "checkedout"):
                return {
                    "pool_size_limit": int(pool.size()),
                    "idle_in_pool": int(pool.checkedin()),
                    "checked_out": int(pool.checkedout()),
                    "overflow_connections": int(pool.overflow()),
                }
            return {
                "pool_size_limit": 0,
                "idle_in_pool": 0,
                "checked_out": 0,
                "overflow_connections": 0,
            }

        for tenant, engine in write_items:
            stats[f"{tenant}:write"] = _pool_stats(engine)

        for tenant, engines in read_items:
            write_engine = write_by_tenant.get(tenant)
            for i, engine in enumerate(engines):
                if engine is write_engine:
                    continue
                stats[f"{tenant}:read:{i}"] = _pool_stats(engine)

        return stats

    def _get_credentials_for_tenant(self, tenant: str) -> Dict[str, Any]:
        """
        Get database credentials for a tenant.

        Secret JSON shape (backward compatible)::

            {
              "user": "...",
              "password": "...",
              "host": "...",              # write / primary (required)
              "database": "...",
              "port": "5432",             # optional
              "read_hosts": ["h1", "h2"], # optional replicas (same user/password/db)
              "read_urls": ["postgresql://..."]  # optional full URLs (alternative)
            }

        If neither ``read_hosts`` nor ``read_urls`` is set, reads use the write host.
        """
        secret_payload_str = get_tenant_db_secret(tenant)
        if not secret_payload_str:
            raise RuntimeError(f"Tenant DB secret not configured for tenant '{tenant}'")

        try:
            secret_payload = json.loads(secret_payload_str)
        except json.JSONDecodeError as exc:
            logger.exception(
                "Unable to retrieve database credentials for tenant %s", tenant
            )
            raise RuntimeError(
                f"Unable to retrieve database credentials for tenant '{tenant}': {str(exc)}"
            ) from exc

        username = secret_payload.get("user")
        password = secret_payload.get("password")
        host = secret_payload.get("host")
        port = str(secret_payload.get("port") or "5432")
        database = secret_payload.get("database")

        write_creds: Dict[str, Any] = {
            "username": username,
            "password": password,
            "host": host,
            "port": port,
            "database": database,
        }

        read_creds_list: List[Dict[str, Any]] = []

        read_urls = secret_payload.get("read_urls") or secret_payload.get("read_url")
        if isinstance(read_urls, str):
            read_urls = [u.strip() for u in read_urls.split(",") if u.strip()]
        if read_urls:
            for url in read_urls:
                read_creds_list.append({"url": url})
        else:
            read_hosts = secret_payload.get("read_hosts") or secret_payload.get("read_host")
            if isinstance(read_hosts, str):
                read_hosts = [h.strip() for h in read_hosts.split(",") if h.strip()]
            if read_hosts:
                for read_host in read_hosts:
                    read_creds_list.append(
                        {
                            "username": username,
                            "password": password,
                            "host": read_host,
                            "port": port,
                            "database": database,
                        }
                    )

        return {"write": write_creds, "read": read_creds_list}

    def _create_engine(self, credentials: Dict[str, Any]) -> Engine:
        url = credentials.get("url") or self._build_connection_url(credentials)
        host_label = credentials.get("host") or url
        logger.debug("Creating PostgreSQL engine for %s", host_label)
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
