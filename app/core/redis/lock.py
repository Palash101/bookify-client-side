from __future__ import annotations

import logging
import secrets
import time
from contextlib import contextmanager
from typing import Iterator

from redis.exceptions import RedisError

from app.core.redis.client import get_redis_client

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 30
DEFAULT_RETRY_DELAY_SECONDS = 0.1

# Release only when the stored token matches, so a lock whose TTL expired and
# was re-acquired by someone else is never deleted by the previous owner.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

# Same ownership check, but extends the TTL instead of deleting.
_EXTEND_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


SEPARATOR = ":"
LOCK = "lock"


def lock_key(name: str, *parts: object) -> str:
    """``lock:{name}[:{part}...]`` — pass a tenant id as a part to scope one."""
    return SEPARATOR.join(
        str(part) for part in (LOCK, name, *parts) if part not in (None, "")
    )


class LockError(RuntimeError):
    """Raised when a required lock could not be acquired."""


class RedisLock:
    """
    A best-effort distributed lock backed by ``SET key token NX PX ttl``.

    When Redis is disabled (``REDIS_ENABLED`` false) the lock is a no-op and
    always reports success, so callers keep working in local/single-instance
    setups.
    """

    def __init__(
        self,
        name: str,
        *parts: object,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        blocking_timeout: float | None = None,
        retry_delay: float = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self.key = lock_key(name, *parts)
        self.ttl_seconds = ttl_seconds
        self.blocking_timeout = blocking_timeout
        self.retry_delay = retry_delay
        self.token: str | None = None

    @property
    def _ttl_ms(self) -> int:
        return max(1, int(self.ttl_seconds * 1000))

    def acquire(self) -> bool:
        """
        Try to take the lock.

        Returns True immediately on success. With ``blocking_timeout`` set, keep
        retrying until the timeout elapses; otherwise fail fast.
        """
        client = get_redis_client()
        if client is None:
            self.token = None
            return True

        token = secrets.token_hex(16)
        deadline = (
            None if self.blocking_timeout is None
            else time.monotonic() + self.blocking_timeout
        )

        while True:
            try:
                if client.set(self.key, token, nx=True, px=self._ttl_ms):
                    self.token = token
                    return True
            except RedisError:
                logger.exception("Redis error while acquiring lock %s", self.key)
                return False

            if deadline is None or time.monotonic() >= deadline:
                return False
            time.sleep(min(self.retry_delay, max(0.0, deadline - time.monotonic())))

    def release(self) -> bool:
        """Release the lock if this instance still owns it."""
        client = get_redis_client()
        if client is None or self.token is None:
            self.token = None
            return True

        try:
            released = bool(client.eval(_RELEASE_SCRIPT, 1, self.key, self.token))
        except RedisError:
            logger.exception("Redis error while releasing lock %s", self.key)
            released = False
        finally:
            self.token = None
        return released

    def extend(self, ttl_seconds: float | None = None) -> bool:
        """Extend the lock TTL while this instance still owns it."""
        client = get_redis_client()
        if client is None or self.token is None:
            return client is None

        ttl_ms = max(1, int((ttl_seconds or self.ttl_seconds) * 1000))
        try:
            return bool(client.eval(_EXTEND_SCRIPT, 1, self.key, self.token, ttl_ms))
        except RedisError:
            logger.exception("Redis error while extending lock %s", self.key)
            return False

    def locked(self) -> bool:
        """Return True when the lock key currently exists (owned by anyone)."""
        client = get_redis_client()
        if client is None:
            return False
        try:
            return bool(client.exists(self.key))
        except RedisError:
            logger.exception("Redis error while checking lock %s", self.key)
            return False

    def __enter__(self) -> "RedisLock":
        if not self.acquire():
            raise LockError(f"Could not acquire lock {self.key}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@contextmanager
def redis_lock(
    name: str,
    *parts: object,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    blocking_timeout: float | None = None,
    retry_delay: float = DEFAULT_RETRY_DELAY_SECONDS,
    raise_on_failure: bool = True,
) -> Iterator[bool]:
    """
    Context manager wrapper around :class:`RedisLock`.

    Yields True when the lock was taken. With ``raise_on_failure`` false it
    yields False instead of raising, letting the caller skip the guarded work.

        with redis_lock("org-cache", org_id, ttl_seconds=10) as acquired:
            if acquired:
                cache_organization_data(...)
    """
    lock = RedisLock(
        name,
        *parts,
        ttl_seconds=ttl_seconds,
        blocking_timeout=blocking_timeout,
        retry_delay=retry_delay,
    )
    acquired = lock.acquire()
    if not acquired and raise_on_failure:
        raise LockError(f"Could not acquire lock {lock.key}")
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()
