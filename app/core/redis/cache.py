from __future__ import annotations

import json
import logging
import random
import threading
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, TypeVar
from uuid import UUID

from pydantic import BaseModel
from redis.exceptions import RedisError

from app.core.redis.client import get_redis_client, record_failure, record_success
from app.core.redis.lock import redis_lock
from app.core.settings import get_settings

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

# Single-flight defaults: hold the rebuild lock a little longer than a slow
# loader takes. The lock is never waited on -- see get_or_set.
FILL_LOCK = "fill"
FILL_LOCK_TTL = 10.0


class _Stats:
    """Hit/miss/error counters, for a metrics or health endpoint."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.hits = self.misses = self.errors = self.writes = 0

    def record(self, field: str) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            reads = self.hits + self.misses
            return {
                "hits": self.hits,
                "misses": self.misses,
                "writes": self.writes,
                "errors": self.errors,
                "hit_rate": round(self.hits / reads, 4) if reads else None,
            }

    def reset(self) -> None:
        with self._lock:
            self.hits = self.misses = self.errors = self.writes = 0


stats = _Stats()


def key_prefix() -> str:
    """Optional global prefix, e.g. ``v2``, to invalidate every key at once."""
    return get_settings().REDIS_KEY_PREFIX.strip().strip(":")


def apply_prefix(key: str) -> str:
    prefix = key_prefix()
    return f"{prefix}:{key}" if prefix else key


def jittered_ttl(ttl: int) -> int:
    """
    Spread expiries so keys written together do not expire together.

    Without this, everything cached during one cold start expires in the same
    second and stampedes the database.
    """
    jitter = get_settings().REDIS_TTL_JITTER
    if ttl <= 0 or jitter <= 0:
        return ttl
    return max(1, int(ttl * (1 + random.uniform(-jitter, jitter))))


def default_ttl() -> int:
    return get_settings().REDIS_ORG_CACHE_TTL_SECONDS


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def _encode(value: Any) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value, default=_json_default)


class RedisCache:
    """
    Cache-aside helper over Redis, addressed by explicit keys.

    Every method is a no-op returning ``None``/``0``/``False`` when Redis is
    disabled, so callers never branch on availability.
    """

    def get(self, key: str, model: type[ModelT] | None = None) -> Any:
        """Read a value, validating it into *model* when one is given."""
        client = get_redis_client()
        if client is None:
            return None
        key = apply_prefix(key)
        try:
            raw = client.get(key)
            record_success()
        except RedisError as exc:
            record_failure()
            stats.record("errors")
            # One line, no traceback: during an outage this fires on every
            # request, and the breaker already logs the transition.
            logger.warning("Redis GET failed for %s: %s", key, exc)
            return None
        if raw is None:
            stats.record("misses")
            return None
        stats.record("hits")
        try:
            decoded = json.loads(raw)
            return model.model_validate(decoded) if model else decoded
        except Exception:
            stats.record("errors")
            # A stale or malformed payload must not break the caller: drop it
            # and let the next read repopulate from the loader.
            logger.warning("Discarding unusable cache value at %s", key)
            self._delete_raw(key)
            return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """Store a value with a TTL. ``ttl <= 0`` stores it without expiry."""
        client = get_redis_client()
        if client is None:
            return False
        ttl = jittered_ttl(default_ttl() if ttl is None else ttl)
        try:
            client.set(apply_prefix(key), _encode(value), **({"ex": ttl} if ttl > 0 else {}))
            record_success()
            stats.record("writes")
            return True
        except RedisError as exc:
            record_failure()
            stats.record("errors")
            logger.warning("Redis SET failed for %s: %s", key, exc)
            return False
        except (TypeError, ValueError):
            # A payload that cannot be encoded is a bug, not an outage.
            stats.record("errors")
            logger.exception("Redis SET failed to encode %s", key)
            return False

    def get_or_set(
        self,
        key: str,
        loader: Callable[[], Any],
        *,
        ttl: int | None = None,
        model: type[ModelT] | None = None,
        single_flight: bool = False,
    ) -> Any:
        """Return the cached value, or load it, store it and return it.

        By default concurrent misses each run *loader*, which is the right
        trade for a cheap indexed query: it costs no extra Redis commands.

        Pass ``single_flight=True`` for an expensive rebuild. One caller then
        takes a lock and loads, and the rest re-read the key before loading —
        so a cold key usually does not stampede the database. It costs two
        extra commands, but only on a miss; a cache hit never touches the lock.

        The lock is taken without waiting. Blocking on it would sleep the
        calling thread, which on an event loop stalls every other request in
        the process for far longer than the loader itself takes. A caller that
        loses the race simply loads too: a duplicate query beats a stalled
        worker, and the same applies when Redis is down.
        """
        cached = self.get(key, model)
        if cached is not None:
            return cached

        if not single_flight:
            return self._load_and_store(key, loader, ttl)

        with self.lock(FILL_LOCK, key, ttl=FILL_LOCK_TTL, required=False):
            # Re-read either way: the winner may have raced another filler, and
            # a loser is often looking at a key that was just filled.
            cached = self.get(key, model)
            if cached is not None:
                return cached
            return self._load_and_store(key, loader, ttl)

    def _load_and_store(self, key: str, loader: Callable[[], Any], ttl: int | None) -> Any:
        value = loader()
        if value is not None:
            self.set(key, value, ttl)
        return value

    def delete(self, *keys: str) -> int:
        return self._delete_raw(*[apply_prefix(key) for key in keys])

    def _delete_raw(self, *keys: str) -> int:
        client = get_redis_client()
        if client is None or not keys:
            return 0
        try:
            return int(client.delete(*keys))
        except RedisError as exc:
            record_failure()
            stats.record("errors")
            logger.warning("Redis DEL failed for %s: %s", keys, exc)
            return 0

    def delete_prefix(self, prefix: str, batch_size: int = 500) -> int:
        """Delete every key under a prefix using SCAN (never KEYS — it blocks)."""
        client = get_redis_client()
        if client is None:
            return 0
        deleted, batch = 0, []
        prefix = apply_prefix(prefix)
        try:
            for found in client.scan_iter(match=f"{prefix}*", count=batch_size):
                batch.append(found)
                if len(batch) >= batch_size:
                    deleted += int(client.delete(*batch))
                    batch = []
            if batch:
                deleted += int(client.delete(*batch))
        except RedisError as exc:
            record_failure()
            logger.warning("Redis SCAN/DEL failed for %s*: %s", prefix, exc)
        return deleted

    def expire(self, key: str, ttl: int) -> bool:
        client = get_redis_client()
        if client is None or ttl <= 0:
            return False
        try:
            return bool(client.expire(apply_prefix(key), ttl))
        except RedisError as exc:
            record_failure()
            logger.warning("Redis EXPIRE failed for %s: %s", key, exc)
            return False

    def lock(
        self,
        name: str,
        *parts: object,
        ttl: float = 30,
        wait: float | None = None,
        required: bool = True,
    ):
        """
        A distributed lock, for rebuilds that must not run twice at once.

        ``required=False`` yields False instead of raising when the lock is
        held elsewhere, so a read path can carry on rather than fail.
        """
        return redis_lock(
            name, *parts, ttl_seconds=ttl, blocking_timeout=wait, raise_on_failure=required
        )

    # -- sorted set: a tenant's running sessions, ordered by login time -------

    def zadd(self, key: str, member: str, score: float, ttl: int | None = None) -> int:
        client = get_redis_client()
        if client is None:
            return 0
        try:
            key = apply_prefix(key)
            added = int(client.zadd(key, {member: score}))
            if ttl and ttl > 0:
                client.expire(key, jittered_ttl(ttl))
            return added
        except RedisError as exc:
            record_failure()
            logger.warning("Redis ZADD failed for %s: %s", key, exc)
            return 0

    def zmembers(self, key: str, newest_first: bool = True) -> list[str]:
        """Members in score order (login time)."""
        client = get_redis_client()
        if client is None:
            return []
        try:
            fetch = client.zrevrange if newest_first else client.zrange
            return [str(member) for member in fetch(apply_prefix(key), 0, -1)]
        except RedisError as exc:
            record_failure()
            logger.warning("Redis ZRANGE failed for %s: %s", key, exc)
            return []

    def zrem(self, key: str, *members: str) -> int:
        client = get_redis_client()
        if client is None or not members:
            return 0
        try:
            return int(client.zrem(apply_prefix(key), *members))
        except RedisError as exc:
            record_failure()
            logger.warning("Redis ZREM failed for %s: %s", key, exc)
            return 0


cache = RedisCache()
