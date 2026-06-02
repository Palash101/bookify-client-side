"""
BookingsService.create tests.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.services.bookings_service import BookingsService, _sessions_remaining_from_sale
from tests.conftest import TENANT_ID
from tests.factory import layout_with_seats


class TestCreateFreeBooking:
    def test_create_free_confirmed_booking(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(price=Decimal("0"), booking_counts=0)
        booking = BookingsService.create(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            "free",
            None,
            None,
            notes="Hello",
            gym_config=booking_gym_config,
        )
        db.refresh(gc)

        assert booking.status == "confirmed"
        assert booking.payment_mode == "free"
        assert booking.order_id is not None
        assert booking.notes == "Hello"
        assert gc.booking_counts == 1

    def test_create_duplicate_raises_400(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class()
        BookingsService.create(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            "free",
            None,
            None,
            notes=None,
            gym_config=booking_gym_config,
        )
        with pytest.raises(HTTPException) as exc:
            BookingsService.create(
                db,
                TENANT_ID,
                current_user,
                gc.id,
                "free",
                None,
                None,
                notes=None,
                gym_config=booking_gym_config,
            )
        assert exc.value.status_code == 400


class TestCreatePackageBooking:
    def test_create_package_deducts_session(
        self, db, factory, current_user, booking_gym_config
    ):
        sale = factory.package_sale(user=current_user, sessions_remaining=3)
        gc = factory.gym_class(booking_type="package", price=Decimal("0"))
        booking = BookingsService.create(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            "package",
            sale.id,
            None,
            notes=None,
            gym_config=booking_gym_config,
        )
        db.refresh(sale)

        assert booking.payment_mode == "package"
        assert booking.sessions_deducted == 1
        assert _sessions_remaining_from_sale(sale) == 2


class TestCreateWalletBooking:
    def test_create_wallet_debits_balance(
        self, db, factory, booking_gym_config
    ):
        user = factory.user()
        user.wallet = Decimal("100")
        gc = factory.gym_class(price=Decimal("40"))
        db.flush()

        booking = BookingsService.create(
            db,
            TENANT_ID,
            user,
            gc.id,
            "wallet",
            None,
            None,
            notes=None,
            gym_config=booking_gym_config,
        )
        db.refresh(user)

        assert booking.status == "confirmed"
        assert booking.payment_mode == "wallet"
        assert user.wallet == Decimal("60")


class TestCreateGatewayBooking:
    def test_create_gateway_pending_payment(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(price=Decimal("30"))
        booking = BookingsService.create(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            "gateway",
            None,
            None,
            notes=None,
            gym_config=booking_gym_config,
        )
        assert booking.status == "pending_payment"
        assert booking.payment_mode == "gateway"


class TestCreateWaitlist:
    def test_create_waitlist_when_full(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(max_bookings=2, max_waitings=5, booking_counts=2)
        for _ in range(2):
            factory.booking(user=factory.user(), gym_class=gc, status="confirmed")
        db.flush()

        booking = BookingsService.create(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            "free",
            None,
            None,
            notes=None,
            gym_config=booking_gym_config,
        )
        assert booking.status == "waiting"
        assert booking.waiting_position == 1

    def test_force_waiting_when_slot_available_raises(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(max_bookings=5)
        with pytest.raises(HTTPException) as exc:
            BookingsService.create(
                db,
                TENANT_ID,
                current_user,
                gc.id,
                "free",
                None,
                None,
                notes=None,
                force_waiting=True,
                gym_config=booking_gym_config,
            )
        assert exc.value.status_code == 400
        assert "available slot" in exc.value.detail.lower()


class TestCreateWithLayout:
    def test_create_marks_seat_booked(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(layouts=layout_with_seats(["A1", "A2"]))
        booking = BookingsService.create(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            "free",
            None,
            "A1",
            notes=None,
            gym_config=booking_gym_config,
        )
        db.refresh(gc)

        assert booking.seat_id == "A1"
        seats = {s["id"]: s["status"] for s in gc.layouts["seats"]}
        assert seats["A1"] == "booked"
        assert seats["A2"] == "available"
