from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class EventEnvelope:
    """Canonical event payload published to Pub/Sub (or logged in dev)."""

    event_type: str
    tenant_id: str
    data: dict[str, Any]
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ordering_key: Optional[str] = None
    published_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_bytes(self) -> bytes:
        payload = {
            "event_type": self.event_type,
            "data": self.data,
            "ordering_key": self.ordering_key or "",
            "event_id": self.event_id,
        }
        return json.dumps(payload, default=str).encode("utf-8")

    def pubsub_attributes(self) -> dict[str, str]:
        return {
            "event_type": self.event_type,
            "tenant_id": self.tenant_id,
            "event_id": self.event_id,
        }
