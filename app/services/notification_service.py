"""Booking notification helpers — domain rules on top of :class:`EventPublishService`."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.core.events.event_payloads import build_booking_notification_data
from app.core.events.event_types import (
    CLIENT_BOOKING_CANCELLED,
    CLIENT_BOOKING_CONFIRMED,
    CLIENT_BOOKING_CREATED,
    CLIENT_BOOKING_PENDING_PAYMENT,
    CLIENT_BOOKING_WAITLIST_JOINED,
    CLIENT_BOOKING_WAITLIST_PROMOTED,
)
from app.core.logging import get_logger
from app.models.class_booking import ClassBooking, class_booking_status_value
from app.schemas.gym_config_value import GymConfigValue
from app.services.event_publish_service import EventPublishService, PublishedEvent

log = get_logger(__name__)


class BookingNotificationService:
    @staticmethod
    def resolve_event_type(booking: ClassBooking) -> Optional[str]:
        status = class_booking_status_value(booking.status)
        if status == "waiting":
            return CLIENT_BOOKING_WAITLIST_JOINED
        if status == "pending_payment":
            return CLIENT_BOOKING_PENDING_PAYMENT
        if status == "confirmed":
            return CLIENT_BOOKING_CONFIRMED
        if status == "pending":
            return CLIENT_BOOKING_CREATED
        return None

    @staticmethod
    def _notification_enabled(cfg: GymConfigValue, event_type: str) -> bool:
        ns = cfg.notification_settings
        if event_type in (
            CLIENT_BOOKING_CREATED,
            CLIENT_BOOKING_CONFIRMED,
            CLIENT_BOOKING_PENDING_PAYMENT,
            CLIENT_BOOKING_CANCELLED,
        ):
            return bool(ns.booking_confirmation)
        if event_type == CLIENT_BOOKING_WAITLIST_JOINED:
            return bool(ns.waitlist_updates)
        if event_type == CLIENT_BOOKING_WAITLIST_PROMOTED:
            return bool(ns.waitlist_updates)
        return True

    @staticmethod
    async def publish(
        db: Session,
        *,
        tenant_id: str,
        booking: ClassBooking,
        event_type: str,
        gym_config: Optional[GymConfigValue] = None,
    ) -> Optional[PublishedEvent]:
        cfg = gym_config or GymConfigValue()
        if not BookingNotificationService._notification_enabled(cfg, event_type):
            log.warning(
                "booking_notification_gym_setting_off tenant_id=%s event_type=%s booking_id=%s "
                "(publishing to Pub/Sub anyway; consumer may skip email)",
                tenant_id,
                event_type,
                booking.id,
            )

        return await EventPublishService.publish(
            tenant_id=tenant_id,
            event_type=event_type,
            data=build_booking_notification_data(booking),
            ordering_key=str(tenant_id),
        )

    @staticmethod
    async def publish_for_booking(
        db: Session,
        *,
        tenant_id: str,
        booking: ClassBooking,
        gym_config: Optional[GymConfigValue] = None,
    ) -> Optional[PublishedEvent]:
        event_type = BookingNotificationService.resolve_event_type(booking)
        if not event_type:
            return None
        return await BookingNotificationService.publish(
            db,
            tenant_id=tenant_id,
            booking=booking,
            event_type=event_type,
            gym_config=gym_config,
        )

    @staticmethod
    async def publish_waitlist_joined(
        db: Session,
        *,
        tenant_id: str,
        booking: ClassBooking,
        gym_config: Optional[GymConfigValue] = None,
    ) -> Optional[PublishedEvent]:
        return await BookingNotificationService.publish(
            db,
            tenant_id=tenant_id,
            booking=booking,
            event_type=CLIENT_BOOKING_WAITLIST_JOINED,
            gym_config=gym_config,
        )

    @staticmethod
    async def publish_waitlist_promoted(
        db: Session,
        *,
        tenant_id: str,
        booking: ClassBooking,
        gym_config: Optional[GymConfigValue] = None,
    ) -> Optional[PublishedEvent]:
        return await BookingNotificationService.publish(
            db,
            tenant_id=tenant_id,
            booking=booking,
            event_type=CLIENT_BOOKING_WAITLIST_PROMOTED,
            gym_config=gym_config,
        )

    @staticmethod
    async def publish_confirmed(
        db: Session,
        *,
        tenant_id: str,
        booking: ClassBooking,
        gym_config: Optional[GymConfigValue] = None,
    ) -> Optional[PublishedEvent]:
        return await BookingNotificationService.publish(
            db,
            tenant_id=tenant_id,
            booking=booking,
            event_type=CLIENT_BOOKING_CONFIRMED,
            gym_config=gym_config,
        )

    @staticmethod
    async def publish_pending_payment(
        db: Session,
        *,
        tenant_id: str,
        booking: ClassBooking,
        gym_config: Optional[GymConfigValue] = None,
    ) -> Optional[PublishedEvent]:
        return await BookingNotificationService.publish(
            db,
            tenant_id=tenant_id,
            booking=booking,
            event_type=CLIENT_BOOKING_PENDING_PAYMENT,
            gym_config=gym_config,
        )

    @staticmethod
    async def publish_cancelled(
        db: Session,
        *,
        tenant_id: str,
        booking: ClassBooking,
        gym_config: Optional[GymConfigValue] = None,
    ) -> Optional[PublishedEvent]:
        return await BookingNotificationService.publish(
            db,
            tenant_id=tenant_id,
            booking=booking,
            event_type=CLIENT_BOOKING_CANCELLED,
            gym_config=gym_config,
        )


# Backwards-compatible alias
NotificationService = BookingNotificationService
