"""Event publishers. One topic for the whole system; the consumer fans out internally.

`ConsolePublisher` is used for local dev (no GCP project). `GooglePubSubPublisher`
talks to Cloud Pub/Sub. The Pub/Sub client is synchronous, so publishes are pushed to
a worker thread to avoid blocking the asyncio event loop.
"""
from __future__ import annotations

import abc
import asyncio
import os
import uuid
from functools import lru_cache
from typing import Any, Optional

from app.core.events.envelope import EventEnvelope
from app.core.logging import get_logger
from app.core.settings import settings

log = get_logger(__name__)


class EventPublisher(abc.ABC):
    @abc.abstractmethod
    async def publish(self, event: EventEnvelope) -> str:
        """Publish one envelope; returns the broker message id."""


class ConsolePublisher(EventPublisher):
    """Logs events instead of publishing — handy for local dev and tests."""

    async def publish(self, event: EventEnvelope) -> str:
        to_email = (event.data or {}).get("to", "")
        log.info(
            "pubsub_publish_console event_type=%s to_email=%s event_id=%s payload=%s",
            event.event_type,
            to_email,
            event.event_id,
            event.to_bytes().decode("utf-8"),
        )
        return f"console-{event.event_id}"


class GooglePubSubPublisher(EventPublisher):
    def __init__(self) -> None:
        if settings.PUBSUB_EMULATOR_HOST:
            os.environ["PUBSUB_EMULATOR_HOST"] = settings.PUBSUB_EMULATOR_HOST

        from google.cloud import pubsub_v1  # imported lazily so dev doesn't need creds

        publisher_options = pubsub_v1.types.PublisherOptions(
            enable_message_ordering=settings.PUBSUB_ENABLE_MESSAGE_ORDERING,
        )
        self._client = pubsub_v1.PublisherClient(publisher_options=publisher_options)
        self._topic_path = self._client.topic_path(
            settings.GCP_PROJECT_ID, settings.PUBSUB_TOPIC_ID
        )

    def _publish_sync(self, event: EventEnvelope) -> str:
        publish_kwargs: dict[str, Any] = event.pubsub_attributes()
        if settings.PUBSUB_ENABLE_MESSAGE_ORDERING and event.ordering_key:
            publish_kwargs["ordering_key"] = event.ordering_key

        future = self._client.publish(
            self._topic_path,
            event.to_bytes(),
            **publish_kwargs,
        )
        return future.result()

    async def publish(self, event: EventEnvelope) -> str:
        to_email = (event.data or {}).get("to", "")
        log.info(
            "pubsub_publish_start event_type=%s topic=%s/%s to_email=%s event_id=%s payload=%s",
            event.event_type,
            settings.GCP_PROJECT_ID,
            settings.PUBSUB_TOPIC_ID,
            to_email,
            event.event_id,
            event.to_bytes().decode("utf-8"),
        )
        message_id = await asyncio.to_thread(self._publish_sync, event)
        log.info(
            "pubsub_publish_ok event_type=%s to_email=%s event_id=%s message_id=%s topic=%s/%s",
            event.event_type,
            to_email,
            event.event_id,
            message_id,
            settings.GCP_PROJECT_ID,
            settings.PUBSUB_TOPIC_ID,
        )
        return message_id


@lru_cache
def get_publisher() -> EventPublisher:
    if settings.publisher_is_console:
        return ConsolePublisher()
    return GooglePubSubPublisher()


async def publish_event(
    event_type: str,
    tenant_id: str,
    data: dict[str, Any],
    *,
    ordering_key: Optional[str] = None,
    event_id: Optional[str] = None,
) -> str:
    """Build an envelope and publish it via the configured publisher."""
    envelope = EventEnvelope(
        event_type=event_type,
        tenant_id=tenant_id,
        data=data,
        ordering_key=ordering_key,
        event_id=event_id or str(uuid.uuid4()),
    )
    return await get_publisher().publish(envelope)
