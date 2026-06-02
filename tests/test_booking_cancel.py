"""
Booking cancellation tests — trainhub-style (db + factory + service layer).
"""

from __future__ import annotations

import uuid
from datetime import date, time, timedelta

import pytest
from fastapi import HTTPException

from app.schemas.gym_config_value import BookingSettingsConfig, GymConfigValue
from app.services.bookings_service import (
    BookingsService,
    _sessions_remaining_from_sale,
)
from tests.conftest import TENANT_ID


class TestCancelBookingBasics:
    def test_cancel_confirmed_booking(self, db, factory, current_user, gym_config):
        gc = factory.gym_class(booking_counts=1)
        booking = factory.booking(
            user=current_user,
            gym_class=gc,
            status="confirmed",
        )
        db.flush()

        result = BookingsService.cancel(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            booking.id,
            reason="Changed plans",
            gym_config=gym_config,
        )
        db.refresh(gc)
        db.refresh(result)

        assert result.status == "cancelled"
        assert result.cancellation_reason == "Changed plans"
        assert result.cancelled_by_user_id == current_user.id
        assert gc.booking_counts == 0

    def test_cancel_unknown_booking_404(self, db, factory, current_user, gym_config):
        gc = factory.gym_class()
        with pytest.raises(HTTPException) as exc:
            BookingsService.cancel(
                db,
                TENANT_ID,
                current_user,
                gc.id,
                uuid.uuid4(),
                reason=None,
                gym_config=gym_config,
            )
        assert exc.value.status_code == 404

    def test_cancel_already_cancelled_400(self, db, factory, current_user, gym_config):
        gc = factory.gym_class()
        booking = factory.booking(
            user=current_user,
            gym_class=gc,
            status="cancelled",
        )
        db.flush()

        with pytest.raises(HTTPException) as exc:
            BookingsService.cancel(
                db,
                TENANT_ID,
                current_user,
                gc.id,
                booking.id,
                reason=None,
                gym_config=gym_config,
            )
        assert exc.value.status_code == 400
        assert "already cancelled" in exc.value.detail.lower()

    def test_cancel_wrong_tenant_404(self, db, factory, current_user, gym_config):
        from app.models.tenant import Tenant

        db.add(Tenant(id="other-tenant", business_name="Other", status="active"))
        db.flush()
        other = factory.user(tenant_id="other-tenant")
        gc = factory.gym_class()
        booking = factory.booking(user=other, gym_class=gc, status="confirmed")
        db.flush()

        with pytest.raises(HTTPException) as exc:
            BookingsService.cancel(
                db,
                "wrong-tenant",
                current_user,
                gc.id,
                booking.id,
                reason=None,
                gym_config=gym_config,
            )
        assert exc.value.status_code == 404


class TestCancelPackageRefund:
    def test_refund_sessions_on_package_cancel(self, db, factory, current_user, gym_config):
        sale = factory.sale(user=current_user, sessions_remaining=4)
        gc = factory.gym_class(booking_counts=1)
        booking = factory.booking(
            user=current_user,
            gym_class=gc,
            status="confirmed",
            payment_mode="package",
            sessions_deducted=1,
            user_package_purchase_id=sale.id,
        )
        db.flush()

        BookingsService.cancel(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            booking.id,
            reason="Refund test",
            gym_config=gym_config,
        )
        db.refresh(sale)

        assert _sessions_remaining_from_sale(sale) == 5

    def test_no_refund_when_sessions_deducted_zero(self, db, factory, current_user, gym_config):
        sale = factory.sale(user=current_user, sessions_remaining=4)
        gc = factory.gym_class()
        booking = factory.booking(
            user=current_user,
            gym_class=gc,
            status="confirmed",
            payment_mode="package",
            sessions_deducted=0,
            user_package_purchase_id=sale.id,
        )
        db.flush()

        BookingsService.cancel(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            booking.id,
            reason=None,
            gym_config=gym_config,
        )
        db.refresh(sale)

        assert _sessions_remaining_from_sale(sale) == 4


class TestCancelCancellationWindow:
    def test_rejects_cancel_inside_window(self, db, factory, current_user):
        tomorrow = date.today() + timedelta(days=1)
        gc = factory.gym_class(class_date=tomorrow, start_time=time(10, 0))
        booking = factory.booking(
            user=current_user,
            gym_class=gc,
            status="confirmed",
        )
        db.flush()

        strict_config = GymConfigValue(
            booking_settings=BookingSettingsConfig(
                allow_late_cancellations=False,
                cancellation_window_hours=48,
            )
        )

        with pytest.raises(HTTPException) as exc:
            BookingsService.cancel(
                db,
                TENANT_ID,
                current_user,
                gc.id,
                booking.id,
                reason=None,
                gym_config=strict_config,
            )
        assert exc.value.status_code == 400
        assert "Cancellation allowed only" in exc.value.detail

    def test_rejects_cancel_after_class_started(self, db, factory, current_user):
        yesterday = date.today() - timedelta(days=1)
        gc = factory.gym_class(class_date=yesterday, start_time=time(9, 0))
        booking = factory.booking(
            user=current_user,
            gym_class=gc,
            status="confirmed",
        )
        db.flush()

        config = GymConfigValue(
            booking_settings=BookingSettingsConfig(
                allow_late_cancellations=False,
                cancellation_window_hours=0,
            )
        )

        with pytest.raises(HTTPException) as exc:
            BookingsService.cancel(
                db,
                TENANT_ID,
                current_user,
                gc.id,
                booking.id,
                reason=None,
                gym_config=config,
            )
        assert exc.value.status_code == 400
        assert "class already started" in exc.value.detail.lower()


class TestCancelWaitlistPromotion:
    def test_cancel_promotes_next_waiting_booking(
        self, db, factory, booking_gym_config
    ):
        confirmed_user = factory.user()
        waiting_user = factory.user()
        gc = factory.gym_class(max_bookings=1, max_waitings=5, booking_counts=1)
        confirmed = factory.booking(
            user=confirmed_user, gym_class=gc, status="confirmed"
        )
        waiting = factory.booking(
            user=waiting_user, gym_class=gc, status="waiting", waiting_position=1
        )
        db.flush()

        BookingsService.cancel(
            db,
            TENANT_ID,
            confirmed_user,
            gc.id,
            confirmed.id,
            reason="Free slot",
            gym_config=booking_gym_config,
        )
        db.refresh(waiting)
        db.refresh(gc)

        assert waiting.status == "confirmed"
        assert waiting.waiting_position is None
        assert waiting.promoted_from_waiting_at is not None
        assert gc.booking_counts == 1


class TestCancelSeatRelease:
    def test_cancel_releases_layout_seat(
        self, db, factory, current_user, gym_config
    ):
        from tests.factory import layout_with_seats

        gc = factory.gym_class(
            booking_counts=1,
            layouts=layout_with_seats(["A1"], statuses=["booked"]),
        )
        booking = factory.booking(
            user=current_user,
            gym_class=gc,
            status="confirmed",
            seat_id="A1",
        )
        db.flush()

        BookingsService.cancel(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            booking.id,
            reason=None,
            gym_config=gym_config,
        )
        db.refresh(gc)

        seats = {s["id"]: s["status"] for s in gc.layouts["seats"]}
        assert seats["A1"] == "available"
