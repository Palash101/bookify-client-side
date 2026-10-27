"""
Regression tests for fix/live-bugs:

- verify_password accepts both raw bcrypt (this service) and passlib
  bcrypt_sha256 (written by the authentication service).
- SecretManager.get_redis_secret exists and the Redis config resolver uses it.
- Default Pub/Sub topic is the one the activities consumer listens on.
- Publishers never log the event payload / recipient.
- Unhandled DB errors are not echoed to clients.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
import httpx
from passlib.context import CryptContext
from sqlalchemy.exc import OperationalError

from app.core import secret_manager as sm_module
from app.core.security import get_password_hash, verify_password

PASSWORD = "Pytest@12345"

# Exactly what fitnez_authentication/app/core/security.py uses.
AUTH_CTX = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated="auto")


# --------------------------------------------------------------------------- #
# Password verification
# --------------------------------------------------------------------------- #


def test_verify_password_accepts_auth_service_bcrypt_sha256_hash():
    hashed = AUTH_CTX.hash(PASSWORD)
    assert hashed.startswith("$bcrypt-sha256$")
    assert verify_password(PASSWORD, hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_verify_password_accepts_raw_bcrypt_hash():
    hashed = get_password_hash(PASSWORD)
    assert hashed.startswith("$2")  # format unchanged for new hashes
    assert verify_password(PASSWORD, hashed) is True
    assert verify_password("wrong-password", hashed) is False
    # The auth service can also verify what this service writes.
    assert AUTH_CTX.verify(PASSWORD, hashed) is True


@pytest.mark.parametrize("bad", [None, "", "not-a-hash", "$bcrypt-sha256$garbage"])
def test_verify_password_rejects_missing_or_malformed_hash(bad):
    assert verify_password(PASSWORD, bad) is False


# --------------------------------------------------------------------------- #
# Redis secret
# --------------------------------------------------------------------------- #


@pytest.fixture
def fresh_secret_manager(monkeypatch):
    monkeypatch.setattr(sm_module.SecretManager, "_instance", None)
    yield sm_module.get_secret_manager()
    monkeypatch.setattr(sm_module.SecretManager, "_instance", None)


def test_get_redis_secret_returns_none_without_template(monkeypatch, fresh_secret_manager):
    monkeypatch.setattr(sm_module.settings, "REDIS_SECRET_TEMPLATE", None, raising=False)
    assert fresh_secret_manager.get_redis_secret() is None


def test_get_redis_secret_fetches_and_caches(monkeypatch, fresh_secret_manager):
    payload = json.dumps({"host": "redis.example", "port": 6380, "password": "pw", "db": 2})
    calls = []

    def fake_access(project_id, secret_id, client):
        calls.append((project_id, secret_id))
        return payload

    monkeypatch.setattr(sm_module.settings, "REDIS_SECRET_TEMPLATE", "redis-creds", raising=False)
    monkeypatch.setattr(sm_module.settings, "GCP_PROJECT_ID", "proj-x")
    monkeypatch.setattr(fresh_secret_manager, "_get_client", lambda: object())
    monkeypatch.setattr(fresh_secret_manager, "_access_latest_secret", fake_access)

    assert fresh_secret_manager.get_redis_secret() == payload
    assert fresh_secret_manager.get_redis_secret() == payload
    assert calls == [("proj-x", "redis-creds")]  # second call served from cache


def test_get_redis_secret_returns_none_on_lookup_error(monkeypatch, fresh_secret_manager):
    def boom(*a, **k):
        raise RuntimeError("denied")

    monkeypatch.setattr(sm_module.settings, "REDIS_SECRET_TEMPLATE", "redis-creds", raising=False)
    monkeypatch.setattr(sm_module.settings, "GCP_PROJECT_ID", "proj-x")
    monkeypatch.setattr(fresh_secret_manager, "_get_client", lambda: object())
    monkeypatch.setattr(fresh_secret_manager, "_access_latest_secret", boom)
    assert fresh_secret_manager.get_redis_secret() is None


def test_redis_config_uses_secret_when_template_set(monkeypatch, fresh_secret_manager):
    """Previously raised AttributeError: SecretManager had no get_redis_secret."""
    from app.core.redis import client as redis_client

    payload = json.dumps({"host": "redis.example", "port": 6380, "password": "pw", "db": 2})
    s = redis_client.get_settings()
    monkeypatch.setattr(s, "MODE", "production")
    monkeypatch.setattr(s, "REDIS_SECRET_TEMPLATE", "redis-creds", raising=False)
    monkeypatch.setattr(sm_module.settings, "REDIS_SECRET_TEMPLATE", "redis-creds", raising=False)
    monkeypatch.setattr(sm_module.settings, "GCP_PROJECT_ID", "proj-x")
    monkeypatch.setattr(fresh_secret_manager, "_get_client", lambda: object())
    monkeypatch.setattr(fresh_secret_manager, "_access_latest_secret", lambda *a: payload)

    cfg = redis_client._resolve_redis_config()
    assert cfg["host"] == "redis.example"
    assert cfg["port"] == 6380
    assert cfg["password"] == "pw"
    assert cfg["db"] == 2


# --------------------------------------------------------------------------- #
# Pub/Sub
# --------------------------------------------------------------------------- #


def test_default_pubsub_topic_is_activities_topic(monkeypatch):
    from app.core.settings import Settings

    monkeypatch.delenv("PUBSUB_TOPIC_ID", raising=False)
    assert Settings().PUBSUB_TOPIC_ID == "fitnezstudios-activities"


def test_console_publisher_does_not_log_payload_or_recipient(caplog):
    from app.core.events.envelope import EventEnvelope
    from app.core.events.publisher import ConsolePublisher

    event = EventEnvelope(
        event_type="client.otp.login",
        tenant_id="ORG-201",
        data={"to": "secret-person@example.com", "otp": "123456", "full_name": "Secret Person"},
        ordering_key="ORG-201",
        event_id="evt-1",
    )
    with caplog.at_level(logging.DEBUG):
        asyncio.run(ConsolePublisher().publish(event))
    text = caplog.text
    assert "evt-1" in text and "client.otp.login" in text
    assert "secret-person@example.com" not in text
    assert "123456" not in text
    assert "Secret Person" not in text


# --------------------------------------------------------------------------- #
# Error responses
# --------------------------------------------------------------------------- #


def test_unhandled_db_error_is_not_echoed(app):
    path = "/__pytest_db_error"

    @app.get(path)
    async def _boom():  # pragma: no cover - exercised via client
        raise OperationalError("SELECT secret_col FROM users", {}, Exception("password=hunter2"))

    try:
        async def go():
            # Starlette re-raises after the Exception handler has responded.
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                return await c.get(path, headers={"X-Tenant-Key": "local-dev-org-201"})

        r = asyncio.run(go())
        # DB unreachable / refused: "busy, retry" (503), still without the SQL or values
        assert r.status_code == 503
        body = r.text
        assert "hunter2" not in body and "secret_col" not in body
    finally:
        app.router.routes[:] = [rt for rt in app.router.routes if getattr(rt, "path", None) != path]
        app.openapi_schema = None
