"""
BookingsService.validate tests.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.schemas.gym_config_value import (
    BookingSettingsConfig,
    GymConfigValue,
    PaymentPricingConfig,
)
from app.services.bookings_service import BookingsService
from tests.conftest import TENANT_ID
from tests.factory import layout_with_seats


class TestValidateClassBasics:
    def test_validate_free_class_success(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(price=Decimal("0"))
        outcome = BookingsService.validate(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            "free",
            None,
            None,
            cfg=booking_gym_config,
        )
        assert outcome.ok is True
        assert outcome.proposed_status == "confirmed"
        assert outcome.proceed_to == "confirm"
        assert outcome.checks_map["class_exists"]["pass"] is True

    def test_validate_unknown_class_fails(self, db, factory, current_user, booking_gym_config):
        outcome = BookingsService.validate(
            db,
            TENANT_ID,
            current_user,
            uuid.uuid4(),
            "free",
            None,
            None,
            cfg=booking_gym_config,
        )
        assert outcome.ok is False
        assert outcome.checks_map["class_exists"]["pass"] is False

    def test_validate_cancelled_class_fails(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(status="cancelled")
        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "free", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is False
        assert outcome.checks_map["class_active"]["pass"] is False

    def test_validate_class_already_started_fails(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(class_date=date.today() - timedelta(days=1))
        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "free", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is False
        assert outcome.checks_map["class_not_started"]["pass"] is False


class TestValidateGenderAndPaymentMode:
    def test_validate_gender_mismatch_fails(
        self, db, factory, booking_gym_config
    ):
        user = factory.user(gender="male")
        gc = factory.gym_class(gender="female")
        outcome = BookingsService.validate(
            db, TENANT_ID, user, gc.id, "free", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is False
        assert outcome.checks_map["gender_eligibility"]["pass"] is False

    def test_validate_paid_class_rejects_free_mode(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(price=Decimal("50"))
        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "free", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is False
        assert outcome.checks_map["class_payment_mode"]["pass"] is False

    def test_validate_package_only_class_rejects_wallet(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(booking_type="package_only", price=Decimal("0"))
        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "wallet", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is False
        assert outcome.checks_map["class_payment_mode"]["pass"] is False

    def test_validate_gateway_proceed_to_payment(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(price=Decimal("25"))
        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "gateway", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is True
        assert outcome.proposed_status == "confirmed"
        assert outcome.proceed_to == "payment"


class TestValidateCapacityAndDuplicates:
    def test_validate_already_booked_fails(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(max_bookings=10)
        factory.booking(user=current_user, gym_class=gc, status="confirmed")
        db.flush()

        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "free", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is False
        assert outcome.checks_map["already_booked"]["pass"] is False

    def test_validate_class_full_waitlist_ok(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(max_bookings=2, max_waitings=3, booking_counts=2)
        for _ in range(2):
            factory.booking(user=factory.user(), gym_class=gc, status="confirmed")
        db.flush()

        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "free", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is True
        assert outcome.proposed_status == "waiting"
        assert outcome.proceed_to == "waitlist"
        assert outcome.waiting_position == 1

    def test_validate_class_full_no_waitlist_fails(self, db, factory, current_user):
        gc = factory.gym_class(max_bookings=2, max_waitings=0, booking_counts=2)
        for _ in range(2):
            factory.booking(user=factory.user(), gym_class=gc, status="confirmed")
        db.flush()

        cfg = GymConfigValue(
            booking_settings=BookingSettingsConfig(
                allow_waiting_list=False,
                auto_confirm_booking=True,
            ),
            payment_pricing=PaymentPricingConfig(enable_free_classes=True),
        )
        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "free", None, None, cfg=cfg
        )
        assert outcome.ok is False
        assert outcome.checks_map["capacity"]["pass"] is False

    def test_validate_one_to_one_already_taken_fails(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(max_bookings=1, booking_counts=1)
        other = factory.user()
        factory.booking(user=other, gym_class=gc, status="confirmed")
        db.flush()

        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "free", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is False
        assert outcome.checks_map["one_to_one_available"]["pass"] is False


class TestValidatePackage:
    def test_validate_package_success(
        self, db, factory, current_user, booking_gym_config
    ):
        sale = factory.package_sale(user=current_user, sessions_remaining=5)
        gc = factory.gym_class(booking_type="package", price=Decimal("0"))
        outcome = BookingsService.validate(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            "package",
            sale.id,
            None,
            cfg=booking_gym_config,
        )
        assert outcome.ok is True
        assert outcome.sale is not None
        assert outcome.checks_map["package_sessions"]["remaining"] == 5

    def test_validate_package_no_sessions_fails(
        self, db, factory, current_user, booking_gym_config
    ):
        sale = factory.package_sale(user=current_user, sessions_remaining=0)
        gc = factory.gym_class(booking_type="package", price=Decimal("0"))
        outcome = BookingsService.validate(
            db,
            TENANT_ID,
            current_user,
            gc.id,
            "package",
            sale.id,
            None,
            cfg=booking_gym_config,
        )
        assert outcome.ok is False
        assert outcome.checks_map["package_sessions"]["pass"] is False
        assert outcome.proceed_to == "payment_selection"

    def test_validate_package_missing_sale_id_fails(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(booking_type="package", price=Decimal("0"))
        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "package", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is False
        assert outcome.checks_map["package_valid"]["pass"] is False


class TestValidateWalletAndSeat:
    def test_validate_insufficient_wallet_fails(
        self, db, factory, booking_gym_config
    ):
        user = factory.user()
        user.wallet = Decimal("10")
        gc = factory.gym_class(price=Decimal("50"))
        db.flush()

        outcome = BookingsService.validate(
            db, TENANT_ID, user, gc.id, "wallet", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is False
        assert outcome.checks_map["wallet_balance"]["pass"] is False

    def test_validate_wallet_sufficient_passes(
        self, db, factory, booking_gym_config
    ):
        user = factory.user()
        user.wallet = Decimal("100")
        gc = factory.gym_class(price=Decimal("50"))
        db.flush()

        outcome = BookingsService.validate(
            db, TENANT_ID, user, gc.id, "wallet", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is True

    def test_validate_seat_required_for_layout_class(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(layouts=layout_with_seats(["A1", "A2"]))
        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "free", None, None, cfg=booking_gym_config
        )
        assert outcome.ok is False
        assert outcome.checks_map["seat_selection"]["pass"] is False

    def test_validate_seat_taken_fails(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(
            max_bookings=10,
            layouts=layout_with_seats(["A1", "A2"]),
        )
        factory.booking(
            user=factory.user(),
            gym_class=gc,
            status="confirmed",
            seat_id="A1",
        )
        db.flush()

        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "free", None, "A1", cfg=booking_gym_config
        )
        assert outcome.ok is False
        assert "already taken" in (outcome.checks_map["seat_selection"].get("message") or "")

    def test_validate_available_seat_passes(
        self, db, factory, current_user, booking_gym_config
    ):
        gc = factory.gym_class(layouts=layout_with_seats(["A1"]))
        outcome = BookingsService.validate(
            db, TENANT_ID, current_user, gc.id, "free", None, "A1", cfg=booking_gym_config
        )
        assert outcome.ok is True
        assert outcome.checks_map["seat_selection"]["pass"] is True
