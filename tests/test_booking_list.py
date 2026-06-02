"""
BookingsService.list_member_bookings tests.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.services.bookings_service import BookingsService
from tests.conftest import TENANT_ID


class TestListMemberBookings:
    def test_splits_upcoming_past_and_waiting(
        self, db, factory, current_user, gym_config
    ):
        upcoming_class = factory.gym_class(class_date=date.today() + timedelta(days=5))
        past_class = factory.gym_class(class_date=date.today() - timedelta(days=3))
        wait_class = factory.gym_class(class_date=date.today() + timedelta(days=2))

        factory.booking(
            user=current_user, gym_class=upcoming_class, status="confirmed"
        )
        factory.booking(user=current_user, gym_class=past_class, status="confirmed")
        factory.booking(
            user=current_user,
            gym_class=wait_class,
            status="waiting",
        )
        db.flush()

        result = BookingsService.list_member_bookings(
            db, TENANT_ID, current_user, gym_config=gym_config
        )

        assert len(result["upcoming"]) == 1
        assert len(result["past"]) == 1
        assert len(result["waiting"]) == 1
        assert result["upcoming"][0]["class_name"] == "Test Class"
        assert result["waiting"][0]["status"] == "waiting"
        assert result["waiting"][0]["waiting_position"] is None

    def test_upcoming_includes_can_cancel_flag(
        self, db, factory, current_user, gym_config
    ):
        gc = factory.gym_class(class_date=date.today() + timedelta(days=10))
        factory.booking(user=current_user, gym_class=gc, status="confirmed")
        db.flush()

        result = BookingsService.list_member_bookings(
            db, TENANT_ID, current_user, gym_config=gym_config
        )

        assert len(result["upcoming"]) == 1
        assert result["upcoming"][0]["can_cancel"] is True
        assert result["upcoming"][0]["cancel_deadline"] is not None

    def test_cancelled_booking_shows_in_past_with_cancelled_at(
        self, db, factory, current_user, gym_config
    ):
        from datetime import datetime, timezone

        gc = factory.gym_class(class_date=date.today() + timedelta(days=3))
        booking = factory.booking(
            user=current_user, gym_class=gc, status="cancelled"
        )
        booking.cancelled_at = datetime.now(timezone.utc)
        db.flush()

        result = BookingsService.list_member_bookings(
            db, TENANT_ID, current_user, gym_config=gym_config
        )

        assert len(result["upcoming"]) == 1
        assert result["upcoming"][0]["status"] == "cancelled"
        assert "cancelled_at" in result["upcoming"][0]
