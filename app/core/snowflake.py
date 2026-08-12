"""Twitter-style Snowflake ID generator (64-bit, time-ordered).

Layout (MSB → LSB):
  1 unused | 41 timestamp ms | 5 datacenter | 5 worker | 12 sequence
"""

from __future__ import annotations

import os
import threading
import time

# Custom epoch: 2024-01-01T00:00:00.000Z
DEFAULT_EPOCH_MS = 1704067200000

WORKER_ID_BITS = 5
DATACENTER_ID_BITS = 5
SEQUENCE_BITS = 12

MAX_WORKER_ID = (1 << WORKER_ID_BITS) - 1
MAX_DATACENTER_ID = (1 << DATACENTER_ID_BITS) - 1
MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1

WORKER_ID_SHIFT = SEQUENCE_BITS
DATACENTER_ID_SHIFT = SEQUENCE_BITS + WORKER_ID_BITS
TIMESTAMP_SHIFT = SEQUENCE_BITS + WORKER_ID_BITS + DATACENTER_ID_BITS


class SnowflakeGenerator:
    """Thread-safe Snowflake ID generator for a single worker."""

    def __init__(
        self,
        worker_id: int = 1,
        datacenter_id: int = 1,
        epoch_ms: int = DEFAULT_EPOCH_MS,
    ) -> None:
        if not 0 <= worker_id <= MAX_WORKER_ID:
            raise ValueError(f"worker_id must be 0..{MAX_WORKER_ID}")
        if not 0 <= datacenter_id <= MAX_DATACENTER_ID:
            raise ValueError(f"datacenter_id must be 0..{MAX_DATACENTER_ID}")

        self.worker_id = worker_id
        self.datacenter_id = datacenter_id
        self.epoch_ms = epoch_ms
        self._sequence = 0
        self._last_timestamp = -1
        self._lock = threading.Lock()

    @staticmethod
    def _current_timestamp_ms() -> int:
        return int(time.time() * 1000)

    def _wait_next_millis(self, last_timestamp: int) -> int:
        timestamp = self._current_timestamp_ms()
        while timestamp <= last_timestamp:
            timestamp = self._current_timestamp_ms()
        return timestamp

    def generate(self) -> int:
        with self._lock:
            timestamp = self._current_timestamp_ms()

            if timestamp < self._last_timestamp:
                raise RuntimeError(
                    "Clock moved backwards; refusing to generate snowflake ID"
                )

            if timestamp == self._last_timestamp:
                self._sequence = (self._sequence + 1) & MAX_SEQUENCE
                if self._sequence == 0:
                    timestamp = self._wait_next_millis(self._last_timestamp)
            else:
                self._sequence = 0

            self._last_timestamp = timestamp
            return (
                ((timestamp - self.epoch_ms) << TIMESTAMP_SHIFT)
                | (self.datacenter_id << DATACENTER_ID_SHIFT)
                | (self.worker_id << WORKER_ID_SHIFT)
                | self._sequence
            )


_generator: SnowflakeGenerator | None = None
_generator_lock = threading.Lock()


def get_snowflake_generator() -> SnowflakeGenerator:
    global _generator
    if _generator is None:
        with _generator_lock:
            if _generator is None:
                worker_id = int(os.getenv("SNOWFLAKE_WORKER_ID", "1"))
                datacenter_id = int(os.getenv("SNOWFLAKE_DATACENTER_ID", "1"))
                _generator = SnowflakeGenerator(
                    worker_id=worker_id,
                    datacenter_id=datacenter_id,
                )
    return _generator


def generate_snowflake_id() -> int:
    """Return the next 64-bit snowflake ID."""
    return get_snowflake_generator().generate()


def generate_snowflake_id_str() -> str:
    """Return the next snowflake ID as a decimal string (for VARCHAR columns)."""
    return str(generate_snowflake_id())


BOOKING_REF_PREFIX = "BK-"


def generate_booking_ref() -> str:
    """Human-readable booking ref, e.g. ``BK-185942817304592384``."""
    return f"{BOOKING_REF_PREFIX}{generate_snowflake_id()}"
