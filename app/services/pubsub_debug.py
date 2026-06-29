"""Helpers to publish tenant events and surface Pub/Sub results in API responses."""
from __future__ import annotations

from typing import Any, Optional

from app.core.logging import get_logger
from app.services.event_publish_service import EventPublishService, PublishedEvent

log = get_logger(__name__)


def pubsub_result_dict(
    published: Optional[PublishedEvent],
    *,
    error: Optional[str] = None,
    skipped_reason: Optional[str] = None,
    event_type: Optional[str] = None,
) -> dict[str, str]:
    out: dict[str, str] = {}
    if skipped_reason:
        out["skipped"] = skipped_reason
    if error:
        out["error"] = error
    if event_type and "event_type" not in out:
        out["event_type"] = event_type
    if published is not None:
        out["event_type"] = published.event_type
        out["event_id"] = published.event_id
        out["message_id"] = published.message_id
    return out


async def publish_tenant_event_debug(
    *,
    tenant_id: str,
    event_type: str,
    data: dict[str, Any],
    ordering_key: Optional[str] = None,
) -> dict[str, str]:
    """Publish like OTP; never raises — returns debug fields for HTTP responses."""
    try:
        published = await EventPublishService.publish(
            tenant_id=tenant_id,
            event_type=event_type,
            data=data,
            ordering_key=ordering_key or str(tenant_id),
        )
        log.info(
            "api_pubsub_published tenant_id=%s event_type=%s event_id=%s message_id=%s to=%s",
            tenant_id,
            event_type,
            published.event_id,
            published.message_id,
            data.get("to", ""),
        )
        return pubsub_result_dict(published)
    except Exception as exc:
        log.exception(
            "api_pubsub_publish_failed tenant_id=%s event_type=%s data=%s",
            tenant_id,
            event_type,
            data,
        )
        return pubsub_result_dict(None, error=str(exc), event_type=event_type)
