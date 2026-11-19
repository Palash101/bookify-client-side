"""Attendance lives in ``class_bookings.show`` (None / True / False), never in the status.

* a booking with attendance recorded can't be cancelled by the member
* the no-show booking block counts ``show = False`` (cancelled bookings don't count)
"""
from __future__ import annotations

from datetime import datetime

from app.schemas.gym_config_value import AttendanceCheckInConfig
from app.services.no_show_block import booking_blocked_until

from tests.conftest import TENANT_ID
from tests.test_booking_rules import TZ, _book, _cancel, bf, gym  # noqa: F401  (fixtures)


def _booked(client, bf):
    member = bf.member()
    class_id = bf.gym_class(program_id=bf.program(bf.location()), booking_type="free", days_ahead=3)
    r = _book(client, class_id, member, "free")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    return member, class_id, data.get("booking_id") or data["id"]


def test_checked_in_booking_cannot_be_cancelled(client, bf, gym):
    member, class_id, booking_id = _booked(client, bf)
    bf._exec("update class_bookings set show = true, checkin_time = now() where id = :id", id=booking_id)
    r = _cancel(client, class_id, booking_id, member)
    assert r.status_code == 400 and "checked in" in r.text
    assert bf.booking(booking_id)["status"] == "confirmed"


def test_no_show_booking_cannot_be_cancelled(client, bf, gym):
    member, class_id, booking_id = _booked(client, bf)
    bf._exec("update class_bookings set show = false where id = :id", id=booking_id)
    r = _cancel(client, class_id, booking_id, member)
    assert r.status_code == 400 and "no-show" in r.text


def test_unmarked_booking_can_still_be_cancelled(client, bf, gym):
    member, class_id, booking_id = _booked(client, bf)
    assert _cancel(client, class_id, booking_id, member).status_code == 200


def test_booking_block_counts_show_false(client, bf, gym):
    member, _, booking_id = _booked(client, bf)
    rules = AttendanceCheckInConfig(no_show_penalty="booking_block", no_show_limit=1, no_show_period_days=30)
    today = datetime.now(TZ).date()

    with bf.sf() as db:
        assert booking_blocked_until(db, TENANT_ID, member["id"], rules, today) is None

    bf._exec("update class_bookings set status = 'cancelled', show = false where id = :id", id=booking_id)
    with bf.sf() as db:
        assert booking_blocked_until(db, TENANT_ID, member["id"], rules, today) is None

    bf._exec("update class_bookings set status = 'confirmed' where id = :id", id=booking_id)
    with bf.sf() as db:
        assert booking_blocked_until(db, TENANT_ID, member["id"], rules, today) is not None
