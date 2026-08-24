from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

import redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

from app.core.secret_manager import get_secret_manager
from app.core.settings import get_settings

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None
_client_lock = threading.Lock()
_disabled_logged = False

# Circuit breaker state: after repeated failures, stop dialling Redis for a
# cooldown so requests fail over to the database immediately instead of paying
# the socket timeout on every call.
_breaker_lock = threading.Lock()
_failures = 0
_open_until = 0.0


def _connection_kwargs() -> dict[str, Any]:
    """Shared connection settings for both the URL and host/port paths."""
    settings = get_settings()
    return {
        "decode_responses": True,
        "socket_connect_timeout": settings.REDIS_CONNECT_TIMEOUT,
        "socket_timeout": settings.REDIS_SOCKET_TIMEOUT,
        "socket_keepalive": True,
        # Managed Redis silently drops idle connections; ping periodically so a
        # dead socket is replaced before a request discovers it.
        "health_check_interval": settings.REDIS_HEALTH_CHECK_INTERVAL,
        # Bound the pool: many service instances must not exhaust the server's
        # connection limit.
        "max_connections": settings.REDIS_MAX_CONNECTIONS,
        "retry": Retry(ExponentialBackoff(cap=0.5, base=0.05), settings.REDIS_RETRIES),
        "retry_on_error": [RedisConnectionError, RedisTimeoutError],
    }


def breaker_is_open() -> bool:
    """True while the breaker is tripped and Redis should be skipped."""
    with _breaker_lock:
        return time.monotonic() < _open_until


def record_failure() -> None:
    """Count a failed Redis operation; trip the breaker past the threshold."""
    global _failures, _open_until
    settings = get_settings()
    with _breaker_lock:
        _failures += 1
        if _failures >= settings.REDIS_BREAKER_THRESHOLD and time.monotonic() >= _open_until:
            _open_until = time.monotonic() + settings.REDIS_BREAKER_COOLDOWN
            logger.error(
                "Redis circuit breaker OPEN after %d failures; skipping cache for %ss",
                _failures,
                settings.REDIS_BREAKER_COOLDOWN,
            )


def record_success() -> None:
    """Reset the breaker after a healthy operation."""
    global _failures, _open_until
    if _failures == 0 and _open_until == 0.0:
        return
    with _breaker_lock:
        if _open_until:
            logger.info("Redis circuit breaker CLOSED; cache back in use")
        _failures, _open_until = 0, 0.0


def _ping(client: redis.Redis) -> bool:
    """
    Verify the connection immediately.

    ``redis.Redis(...)`` is lazy — it connects on first command — so without
    this a bad host, password or TLS setting stays invisible until some later
    request swallows the error.
    """
    try:
        client.ping()
        logger.info("Redis connection OK")
        return True
    except Exception as exc:
        logger.error("Redis connection FAILED: %s: %s", type(exc).__name__, exc)
        return False


def _is_local_mode() -> bool:
    return get_settings().MODE.lower() in ("development", "local", "dev")


def _safe_url(url: str) -> str:
    """Strip credentials from a Redis URL so it is safe to log."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    except ValueError:
        return "<redis-url>"


def _resolve_redis_config() -> dict[str, Any]:
    """Build Redis connection parameters for local or GCP environments."""
    settings = get_settings()

    if _is_local_mode():
        return {
            "host": settings.REDIS_HOST,
            "port": settings.REDIS_PORT,
            "password": settings.REDIS_PASSWORD or None,
            "db": settings.REDIS_DB,
            "ssl": settings.REDIS_SSL,
            "decode_responses": True,
        }

    # GCP: prefer secret payload when configured, otherwise fall back to env vars.
    if settings.REDIS_SECRET_TEMPLATE:
        secret_payload_str = get_secret_manager().get_redis_secret()
        if secret_payload_str:
            try:
                secret_payload = json.loads(secret_payload_str)
                return {
                    "host": secret_payload.get("host") or settings.REDIS_HOST,
                    "port": int(secret_payload.get("port") or settings.REDIS_PORT),
                    "password": secret_payload.get("password") or settings.REDIS_PASSWORD or None,
                    "db": int(secret_payload.get("db", settings.REDIS_DB)),
                    "ssl": bool(secret_payload.get("ssl", settings.REDIS_SSL)),
                    "decode_responses": True,
                }
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.exception("Failed to parse Redis secret; falling back to env vars")

    return {
        "host": settings.REDIS_HOST,
        "port": settings.REDIS_PORT,
        "password": settings.REDIS_PASSWORD or None,
        "db": settings.REDIS_DB,
        "ssl": settings.REDIS_SSL,
        "decode_responses": True,
    }


def get_redis_client() -> redis.Redis | None:
    """
    Return a shared Redis client when ``REDIS_ENABLED`` is true, else None.
    """
    global _client, _disabled_logged

    settings = get_settings()
    if not settings.REDIS_ENABLED:
        # Log once: without this, a disabled cache is indistinguishable from a
        # code path that never runs.
        if not _disabled_logged:
            _disabled_logged = True
            logger.warning(
                "Redis is DISABLED (REDIS_ENABLED=%r); all cache calls are no-ops",
                os.getenv("REDIS_ENABLED"),
            )
        return None

    # Breaker open: behave exactly as if Redis were disabled.
    if breaker_is_open():
        return None

    if _client is not None:
        return _client

    with _client_lock:
        if _client is not None:
            return _client

        # A full URL wins over the individual host/port settings; it is the
        # simplest way to point at a managed provider such as Upstash.
        if settings.REDIS_URL:
            logger.info("Connecting to Redis via REDIS_URL (%s)", _safe_url(settings.REDIS_URL))
            _client = redis.Redis.from_url(settings.REDIS_URL, **_connection_kwargs())
            _ping(_client)
            return _client

        config = _resolve_redis_config()
        logger.info("Connecting to Redis at %s:%s", config["host"], config["port"])
        _client = redis.Redis(
            host=config["host"],
            port=config["port"],
            password=config["password"],
            db=config["db"],
            ssl=config["ssl"],
            **_connection_kwargs(),
        )
        _ping(_client)
        return _client
