"""
Integration fixtures: the real app + the local tenant database.

Run inside the dev-env client container (DB, Redis and master org are there):

    docker compose -f docker-compose.yml -f docker-compose.localdb.yml \
        exec -T client python -m pytest -q

Every row a test creates is tagged with a unique "pytest-" name / email and
removed again at teardown. Tests are skipped when the tenant DB is unreachable.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TENANT_ID = os.getenv("TEST_TENANT_ID", "ORG-201")
TENANT_KEY = os.getenv("TEST_TENANT_KEY", "local-dev-org-201")


@pytest.fixture(scope="session")
def app():
    from app.main import app as fastapi_app

    return fastapi_app


@pytest.fixture(scope="session")
def session_factory():
    from app.core.db.session import get_session_factory

    try:
        factory = get_session_factory(TENANT_ID)
        with factory() as db:
            db.execute(text("select 1 from event_reward_grants limit 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"tenant DB {TENANT_ID} not reachable / not migrated: {exc}")
    return factory


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


class SyncASGIClient:
    """
    Minimal sync client over httpx.ASGITransport.

    starlette's TestClient in the pinned FastAPI (0.104) doesn't work with the
    installed httpx (>= 0.28 dropped the `app=` argument), so drive the ASGI app
    directly, one event loop per request.
    """

    def __init__(self, app, headers: Dict[str, str]):
        self.app = app
        self.headers = dict(headers)

    def request(self, method: str, url: str, headers: Optional[Dict[str, str]] = None, **kwargs):
        import asyncio

        import httpx

        merged = {**self.headers, **(headers or {})}

        async def go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                return await c.request(method, url, headers=merged, **kwargs)

        return asyncio.run(go())

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)


@pytest.fixture()
def client(app, session_factory):
    return SyncASGIClient(app, {"X-Tenant-Key": TENANT_KEY})


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Factory:
    """Creates tenant rows for a test and deletes them afterwards."""

    def __init__(self, session_factory):
        self.sf = session_factory
        self.tag = f"pytest-{uuid.uuid4().hex[:10]}"
        self.users: List[uuid.UUID] = []
        self.events: List[uuid.UUID] = []
        self.rewards: List[uuid.UUID] = []
        self.badges: List[uuid.UUID] = []

    def _exec(self, sql: str, **params: Any) -> None:
        with self.sf() as s:
            s.execute(text(sql), params)
            s.commit()

    # ------------------------------------------------------------- users
    def user(self, password: Optional[str] = None) -> Dict[str, Any]:
        from app.core.security import create_access_token, get_password_hash

        uid = uuid.uuid4()
        email = f"{self.tag}-{uid.hex[:6]}@example.com"
        self._exec(
            "insert into users (id, tenant_id, email, first_name, last_name, is_active, password_hash) "
            "values (:id, :t, :e, 'Py', 'Test', true, :pw)",
            id=uid,
            t=TENANT_ID,
            e=email,
            pw=get_password_hash(password) if password else None,
        )
        self.users.append(uid)
        token = create_access_token({"sub": str(uid), "email": email, "tenant_id": TENANT_ID})
        return {"id": uid, "email": email, "token": token, "auth": {"Authorization": f"Bearer {token}"}}

    # ------------------------------------------------------------ events
    def event(
        self,
        *,
        type: str = "challenge",
        status: str = "active",
        starts_at: Optional[datetime] = None,
        ends_at: Optional[datetime] = "default",  # type: ignore[assignment]
        max_participants: Optional[int] = None,
        conditions: Optional[List[Dict[str, Any]]] = None,
        mode: str = "any_booking",
        challenge_type: str = "booking",
        finalized: bool = False,
    ) -> uuid.UUID:
        eid = uuid.uuid4()
        if ends_at == "default":
            ends_at = _now() + timedelta(days=7)
        self._exec(
            "insert into event_events (id, tenant_id, name, type, status, starts_at, ends_at, "
            "max_participants, location, finalized_at) values (:id, :t, :n, "
            "cast(:ty as event_type), cast(:st as event_status), :sa, :ea, :mp, 'pytest', :fa)",
            id=eid,
            t=TENANT_ID,
            n=f"{self.tag} event {len(self.events)}",
            ty=type,
            st=status,
            sa=starts_at or _now() - timedelta(days=1),
            ea=ends_at,
            mp=max_participants,
            fa=_now() if finalized else None,
        )
        self.events.append(eid)
        if type == "challenge":
            import json

            self._exec(
                "insert into event_conditions (event_id, challenge_type, challenge_mode, conditions) "
                "values (:e, cast(:ct as challenge_type_enum), cast(:m as challenge_mode_enum), "
                "cast(:c as jsonb))",
                e=eid,
                ct=challenge_type,
                m=mode,
                c=json.dumps(conditions or [{"minimum_quantity": 3}]),
            )
        return eid

    def reward(self, event_id: uuid.UUID, *, type: str = "wallet", tier: int = 1, name: str = "") -> uuid.UUID:
        rid = uuid.uuid4()
        self._exec(
            "insert into event_rewards (id, tenant_id, name, type, payload) values "
            "(:id, :t, :n, cast(:ty as rewards_type), '{}'::jsonb)",
            id=rid,
            t=TENANT_ID,
            n=name or f"{self.tag} reward {len(self.rewards)}",
            ty=type,
        )
        self.rewards.append(rid)
        self._exec(
            "insert into event_rewards_rel (event_id, reward_id, tier) values (:e, :r, :tier)",
            e=event_id,
            r=rid,
            tier=tier,
        )
        return rid

    def enroll(self, event_id, user_id, **cols: Any) -> None:
        self._exec(
            "insert into event_enroll (id, tenant_id, user_id, event_id, created_at, progress_count, "
            "eligible_at, final_rank) values (gen_random_uuid(), :t, :u, :e, coalesce(:ca, now()), "
            ":pc, :ea, :fr)",
            t=TENANT_ID,
            u=user_id,
            e=event_id,
            ca=cols.get("created_at"),
            pc=cols.get("progress_count", 0),
            ea=cols.get("eligible_at"),
            fr=cols.get("final_rank"),
        )

    def grant(self, event_id, user_id, reward_id, *, tier: int = 1, status: str = "granted") -> uuid.UUID:
        gid = uuid.uuid4()
        self._exec(
            "insert into event_reward_grants (id, tenant_id, event_id, user_id, reward_id, tier, rank, "
            "reward_type, status) values (:id, :t, :e, :u, :r, :tier, :tier, 'wallet', :st)",
            id=gid,
            t=TENANT_ID,
            e=event_id,
            u=user_id,
            r=reward_id,
            tier=tier,
            st=status,
        )
        return gid

    def badge(self, *, icon_path: Optional[str] = None) -> uuid.UUID:
        bid = uuid.uuid4()
        self._exec(
            "insert into event_badges (id, tenant_id, name, icon_path) values (:id, :t, :n, :i)",
            id=bid,
            t=TENANT_ID,
            n=f"{self.tag} badge {len(self.badges)}",
            i=icon_path,
        )
        self.badges.append(bid)
        return bid

    def award_badge(self, user_id, badge_id, event_id=None, awarded_at=None) -> None:
        self._exec(
            "insert into user_badges (tenant_id, user_id, badge_id, event_id, awarded_at) "
            "values (:t, :u, :b, :e, coalesce(:a, now()))",
            t=TENANT_ID,
            u=user_id,
            b=badge_id,
            e=event_id,
            a=awarded_at,
        )

    def cleanup(self) -> None:
        with self.sf() as s:
            if self.events:
                s.execute(text("delete from event_events where id = any(:ids)"), {"ids": self.events})
            if self.rewards:
                s.execute(text("delete from event_reward_grants where reward_id = any(:ids)"), {"ids": self.rewards})
                s.execute(text("delete from event_rewards_rel where reward_id = any(:ids)"), {"ids": self.rewards})
                s.execute(text("delete from event_rewards where id = any(:ids)"), {"ids": self.rewards})
            if self.badges:
                s.execute(text("delete from event_badges where id = any(:ids)"), {"ids": self.badges})
            if self.users:
                s.execute(text("delete from users where id = any(:ids)"), {"ids": self.users})
            s.commit()


@pytest.fixture()
def factory(session_factory):
    f = Factory(session_factory)
    try:
        yield f
    finally:
        f.cleanup()


@pytest.fixture()
def now() -> Callable[[], datetime]:
    return _now
