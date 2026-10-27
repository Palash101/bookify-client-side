"""
The OTP must only travel by email: no auth endpoint may echo it back unless the
dev-only settings.EXPOSE_OTP_IN_RESPONSE flag is on.
"""
from __future__ import annotations

import pytest

from app.core.settings import settings
from app.services.event_publish_service import EventPublishService, PublishedEvent

PASSWORD = "Pytest@12345"


@pytest.fixture(autouse=True)
def no_publish(monkeypatch):
    """Don't send real OTP emails; record what would have been published."""
    sent = []

    async def fake_publish(*, tenant_id, event_type, data, ordering_key=None):
        sent.append({"event_type": event_type, "data": data})
        return PublishedEvent(event_id="e", message_id="m", event_type=event_type, tenant_id=str(tenant_id))

    monkeypatch.setattr(EventPublishService, "publish", staticmethod(fake_publish))
    return sent


def _flows(client, factory):
    """Run every endpoint that generates an OTP; yield (name, response)."""
    user = factory.user(password=PASSWORD)
    reg_email = f"{factory.tag}-reg@example.com"

    r = client.post(
        "/api/v1/client/auth/register",
        json={
            "first_name": "Py",
            "last_name": "Reg",
            "email": reg_email,
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            "terms_accepted": True,
        },
    )
    yield "register", r
    reg_token = r.json().get("token")

    r = client.post("/api/v1/client/auth/login", json={"email": user["email"], "password": PASSWORD})
    yield "login", r

    r = client.post("/api/v1/client/auth/forgot-password", json={"email": user["email"]})
    yield "forgot-password", r

    r = client.post("/api/v1/client/auth/resend-otp", headers={"Authorization": f"Bearer {reg_token}"})
    yield "resend-otp", r


def test_otp_never_in_response_by_default(client, factory, no_publish, monkeypatch):
    monkeypatch.setattr(settings, "EXPOSE_OTP_IN_RESPONSE", False)
    for name, r in _flows(client, factory):
        assert r.status_code == 200, (name, r.text)
        body = r.json()
        assert "otp_code" not in body, name
        assert "token" in body, name
        otp = no_publish[-1]["data"]["otp"]
        assert otp not in r.text, name  # not hidden under another key either
    assert len(no_publish) == 4  # every flow still emails the code


def test_otp_exposed_only_with_dev_flag(client, factory, no_publish, monkeypatch):
    monkeypatch.setattr(settings, "EXPOSE_OTP_IN_RESPONSE", True)
    for name, r in _flows(client, factory):
        assert r.status_code == 200, (name, r.text)
        assert r.json()["otp_code"] == no_publish[-1]["data"]["otp"], name


def test_flag_defaults_to_false():
    from app.core.settings import Settings

    assert Settings.model_fields["EXPOSE_OTP_IN_RESPONSE"].default is False
