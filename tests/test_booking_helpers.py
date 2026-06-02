"""
Unit tests for pure booking helper functions in bookings_service.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.models.gym_class import GymClass
from app.models.sales import Sale
from app.services import bookings_service as svc
from app.services.bookings_service import BookingValidationOutcome
from tests.factory import layout_with_seats


class TestGenderNormalization:
    def test_user_gender_aliases(self):
        assert svc._normalize_user_gender_for_booking("M") == "male"
        assert svc._normalize_user_gender_for_booking("Women") == "female"
        assert svc._normalize_user_gender_for_booking("other") is None
        assert svc._normalize_user_gender_for_booking(None) is None

    def test_class_gender_mixed_and_restricted(self):
        assert svc._normalize_class_gender_for_booking(None) == "mixed"
        assert svc._normalize_class_gender_for_booking("ANY") == "mixed"
        assert svc._normalize_class_gender_for_booking("men") == "male"

    def test_gender_eligibility_messages(self):
        ok, msg = svc._gender_eligibility_message("mixed", None)
        assert ok is True
        assert msg == ""

        ok, msg = svc._gender_eligibility_message("female", None)
        assert ok is False
        assert "gender is required" in msg

        ok, msg = svc._gender_eligibility_message("male", "female")
        assert ok is False
        assert "men only" in msg


class TestClassHelpers:
    def test_is_cancelled_and_inactive(self):
        assert svc._is_cancelled_class("Cancelled") is True
        assert svc._is_cancelled_class("canceled") is True
        assert svc._is_inactive_class("disabled") is True
        assert svc._is_cancelled_class("active") is False

    def test_package_only_booking_type(self):
        assert svc._class_is_package_only("class_package") is True
        assert svc._class_is_package_only("pay_per_class") is False

    def test_effective_capacity_from_layout(self):
        gc = GymClass(
            max_bookings=5,
            layouts={
                "totalSeats": 8,
                "seats": [{"id": "A1", "status": "available"}],
            },
        )
        assert svc._effective_capacity(gc) == 8

    def test_effective_capacity_fallback_max_bookings(self):
        gc = GymClass(max_bookings=12, layouts=None, layout_id=0)
        assert svc._effective_capacity(gc) == 12

    def test_class_starts_at(self):
        gc = GymClass(class_date=date(2026, 6, 15), start_time=time(9, 30))
        starts = svc._class_starts_at(gc, ZoneInfo("UTC"))
        assert starts is not None
        assert starts.hour == 9
        assert starts.minute == 30


class TestSaleSessionHelpers:
    def test_sessions_remaining_from_sale(self):
        sale = Sale(extra_metadata={"sessions_remaining": 3})
        assert svc._sessions_remaining_from_sale(sale) == 3

    def test_sessions_remaining_legacy_keys(self):
        sale = Sale(extra_metadata={"remaining_sessions": 7})
        assert svc._sessions_remaining_from_sale(sale) == 7

    def test_restore_sessions_to_sale(self):
        sale = Sale(extra_metadata={"sessions_remaining": 2})
        svc._restore_sessions_to_sale(sale, 2)
        assert sale.extra_metadata["sessions_remaining"] == 4

    def test_restore_sessions_noop_when_missing_counter(self):
        sale = Sale(extra_metadata={})
        svc._restore_sessions_to_sale(sale, 1)
        assert sale.extra_metadata == {}


class TestLayoutHelpers:
    def test_class_has_layout_from_layouts_json(self):
        gc = GymClass(layouts=layout_with_seats(["A1"]))
        assert svc._class_has_layout(gc) is True

    def test_layout_seat_status_and_update(self):
        gc = GymClass(layouts=layout_with_seats(["A1"], statuses=["available"]))
        status, err = svc._layout_seat_status(gc, "A1")
        assert err is None
        assert status == "available"
        assert svc._set_layout_seat_status(gc, "A1", "booked") is True
        status2, _ = svc._layout_seat_status(gc, "A1")
        assert status2 == "booked"

    def test_layout_seat_not_found(self):
        gc = GymClass(layouts=layout_with_seats(["A1"]))
        status, err = svc._layout_seat_status(gc, "Z9")
        assert status is None
        assert err is not None


class TestFinalizeValidation:
    def test_finalize_success_free_proceed_confirm(self):
        outcome = BookingValidationOutcome(ok=True, proposed_status="confirmed")
        svc._finalize_booking_validation(outcome, "free")
        assert outcome.proceed_to == "confirm"

    def test_finalize_gateway_proceed_payment(self):
        outcome = BookingValidationOutcome(ok=True, proposed_status="confirmed")
        svc._finalize_booking_validation(outcome, "gateway")
        assert outcome.proceed_to == "payment"

    def test_finalize_waitlist_proceed(self):
        outcome = BookingValidationOutcome(ok=True, proposed_status="waiting")
        svc._finalize_booking_validation(outcome, "free")
        assert outcome.proceed_to == "waitlist"


class TestWalletNoteHelper:
    def test_append_bfy_wtxn_note_idempotent(self):
        txn_id = uuid4()
        note = svc._append_bfy_wtxn_note("existing", txn_id, "debit")
        again = svc._append_bfy_wtxn_note(note, txn_id, "debit")
        assert again == note
        assert str(txn_id) in note
