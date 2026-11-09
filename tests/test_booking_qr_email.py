"""The booking QR email: the QR is inside the email, scans at the class, and its link works."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from jose import jwt

from app.api import class_bookings
from app.core.security import create_class_checkin_token

from tests.conftest import TENANT_ID
from tests.test_booking_rules import TZ, _book, bf, gym  # noqa: F401  (fixtures)


@pytest.fixture()
def sent(monkeypatch):
    """Booking emails sent during the test, instead of SMTP."""
    out = []

    async def send_email(**message):
        out.append(message)
        return True

    monkeypatch.setattr(class_bookings.email_service, "send_email", send_email)
    return out


@pytest.fixture()
def qr_booking(client, bf, gym, sent):
    gym(attendance_check_in__enable_qr_code_check_in=True)
    member = bf.member()
    class_id = bf.gym_class(program_id=bf.program(bf.location()), booking_type="free", days_ahead=3)
    r = _book(client, class_id, member, "free")
    assert r.status_code == 200, r.text
    booking_id = r.json()["data"]["booking_id"] if "booking_id" in r.json()["data"] else r.json()["data"]["id"]
    return {"member": member, "class_id": class_id, "booking_id": booking_id}


def _qr_part(message):
    (attachment,) = message["attachments"]
    return attachment


def test_email_carries_the_qr_as_an_inline_png(qr_booking, sent, bf):
    (message,) = sent
    assert 'src="cid:booking-qr"' in message["html_body"]
    part = _qr_part(message)
    assert part["headers"]["Content-ID"] == "<booking-qr>"
    assert part["headers"]["Content-Disposition"].startswith("inline")
    png = part["file"].file.getvalue()
    assert png.startswith(b"\x89PNG")

    token = bf.booking(qr_booking["booking_id"])["checkin_token"]
    assert png == class_bookings._booking_qr_png(token)  # the QR is the booking's check-in token


def test_qr_stays_valid_until_after_the_class(qr_booking, bf):
    token = bf.booking(qr_booking["booking_id"])["checkin_token"]
    expires = datetime.fromtimestamp(jwt.get_unverified_claims(token)["exp"], TZ)
    assert expires > datetime.now(TZ) + timedelta(days=3)  # the class is 3 days ahead


def test_open_qr_link_works_without_signing_in(qr_booking, sent, client):
    link = sent[0]["html_body"].split('<a href="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
    path = link[link.index("/api/v1/client"):]
    assert "/qr/view?" in path
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/svg+xml")


def test_a_qr_token_only_opens_its_own_booking(qr_booking, client):
    other = create_class_checkin_token(
        booking_id=str(uuid.uuid4()), class_id=str(qr_booking["class_id"]),
        tenant_id=TENANT_ID, user_id=str(qr_booking["member"]["id"]),
    )
    path = (f"/api/v1/client/classes/{qr_booking['class_id']}/bookings/{qr_booking['booking_id']}"
            f"/qr/view?tenant_id={TENANT_ID}&token={other}")
    assert client.get(path).status_code == 403


def test_an_expired_stored_qr_is_replaced(qr_booking, bf, client):
    expired = create_class_checkin_token(
        booking_id=str(qr_booking["booking_id"]), class_id=str(qr_booking["class_id"]),
        tenant_id=TENANT_ID, user_id=str(qr_booking["member"]["id"]), expires_delta=timedelta(seconds=-1),
    )
    bf._exec("update class_bookings set checkin_token = :t where id = :id", t=expired, id=qr_booking["booking_id"])

    r = client.get(f"/api/v1/client/classes/{qr_booking['class_id']}/bookings/{qr_booking['booking_id']}/qr",
                   headers=qr_booking["member"]["auth"])
    assert r.status_code == 200, r.text
    fresh = r.json()["data"]["qr_token"]
    assert fresh != expired and bf.booking(qr_booking["booking_id"])["checkin_token"] == fresh
