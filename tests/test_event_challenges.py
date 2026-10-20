"""
Member challenge APIs: enrollment rules (incl. races), GET /events extras,
GET /events/me, DELETE /events/{id}/enroll and GET /me/badges.
"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.services.events_service.events_service import EventsService
from tests.conftest import TENANT_ID

ALREADY = "You are already enrolled in this event"


def _enroll(client, event_id, user):
    return client.post(f"/api/v1/client/events/{event_id}/enroll", headers=user["auth"])


def _my_event(client, factory, user=None):
    """GET /events filtered to this test's events (search by the unique tag)."""
    headers = user["auth"] if user else {}
    r = client.get("/api/v1/client/events", params={"search": factory.tag, "limit": 100}, headers=headers)
    assert r.status_code == 200, r.text
    return {item["id"]: item for item in r.json()["data"]}


# --------------------------------------------------------------- 1. capacity
@pytest.mark.parametrize("max_participants", [None, 0])
def test_max_participants_null_or_zero_is_unlimited(client, factory, max_participants):
    event_id = factory.event(max_participants=max_participants)
    for _ in range(3):
        assert _enroll(client, event_id, factory.user()).status_code == 200


def test_max_participants_full(client, factory):
    event_id = factory.event(max_participants=1)
    assert _enroll(client, event_id, factory.user()).status_code == 200
    r = _enroll(client, event_id, factory.user())
    assert r.status_code == 400
    assert r.json()["message"] == "Event has reached maximum participants"


# ------------------------------------------------------- 2. challenges only
def test_standard_event_cannot_be_joined(client, factory):
    event_id = factory.event(type="standard")
    r = _enroll(client, event_id, factory.user())
    assert r.status_code == 400
    assert r.json()["message"] == "Only challenges can be joined"


# ------------------------------------------------------------ 3. open window
def test_can_join_before_start(client, factory, now):
    event_id = factory.event(starts_at=now() + timedelta(days=2), ends_at=now() + timedelta(days=9))
    assert _enroll(client, event_id, factory.user()).status_code == 200


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "completed"},
        {"status": "inactive"},
        {"status": "draft"},
        {"ends_at": "past"},
        {"finalized": True},
    ],
)
def test_cannot_join_closed_or_finished(client, factory, now, kwargs):
    if kwargs.get("ends_at") == "past":
        kwargs = {"starts_at": now() - timedelta(days=5), "ends_at": now() - timedelta(minutes=1)}
    event_id = factory.event(**kwargs)
    r = _enroll(client, event_id, factory.user())
    assert r.status_code == 400, r.text


def test_unknown_event_404(client, factory):
    import uuid

    assert _enroll(client, uuid.uuid4(), factory.user()).status_code == 404


def test_completed_event_is_readable(db, factory):
    """5. ORM enum knows 'completed' — loading such a row must not crash."""
    from app.models.event_event import EventEvent, EventStatus

    event_id = factory.event(status="completed", finalized=True)
    event = db.query(EventEvent).filter(EventEvent.id == event_id).one()
    assert event.status == EventStatus.completed
    assert event.finalized_at is not None


# -------------------------------------------------------------- 4. races
def test_duplicate_enroll_returns_400(client, factory):
    event_id = factory.event()
    user = factory.user()
    assert _enroll(client, event_id, user).status_code == 200
    r = _enroll(client, event_id, user)
    assert r.status_code == 400
    assert r.json()["message"] == ALREADY


def test_unique_violation_maps_to_already_enrolled(session_factory, factory, monkeypatch):
    """The pre-check misses a concurrent insert -> IntegrityError -> same 400."""
    event_id = factory.event()
    user = factory.user()
    factory.enroll(event_id, user["id"])
    monkeypatch.setattr(EventsService, "_is_enrolled", staticmethod(lambda *a, **k: False))
    with session_factory() as s:
        with pytest.raises(HTTPException) as exc:
            EventsService.enroll_user(s, tenant_id=TENANT_ID, user_id=user["id"], event_id=event_id)
    assert exc.value.status_code == 400
    assert exc.value.detail == ALREADY


def _concurrently(fns):
    barrier = threading.Barrier(len(fns))
    results = [None] * len(fns)

    def run(i, fn):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except HTTPException as exc:
            results[i] = (exc.status_code, exc.detail)
        except Exception as exc:  # pragma: no cover - would be a bug
            results[i] = ("error", repr(exc))

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def _enroll_in_own_session(session_factory, event_id, user_id):
    def fn():
        with session_factory() as s:
            EventsService.enroll_user(s, tenant_id=TENANT_ID, user_id=user_id, event_id=event_id)
            return True

    return fn


def test_concurrent_enroll_same_user(session_factory, factory, db):
    from sqlalchemy import text

    event_id = factory.event()
    user = factory.user()
    results = _concurrently([_enroll_in_own_session(session_factory, event_id, user["id"]) for _ in range(5)])
    assert sum(1 for r in results if r[0] == "ok") == 1, results
    assert all(r == (400, ALREADY) for r in results if r[0] != "ok"), results
    count = db.execute(
        text("select count(*) from event_enroll where event_id = :e"), {"e": event_id}
    ).scalar()
    assert count == 1


def test_concurrent_enroll_respects_capacity(session_factory, factory, db):
    from sqlalchemy import text

    event_id = factory.event(max_participants=2)
    users = [factory.user() for _ in range(6)]
    results = _concurrently([_enroll_in_own_session(session_factory, event_id, u["id"]) for u in users])
    assert sum(1 for r in results if r[0] == "ok") == 2, results
    count = db.execute(
        text("select count(*) from event_enroll where event_id = :e"), {"e": event_id}
    ).scalar()
    assert count == 2


# ---------------------------------------------------------- 7. GET /events
def test_list_events_challenge_rewards_and_is_enrolled(client, factory):
    challenge = factory.event(
        mode="time_based",
        conditions=[
            {"minimum_quantity": 2, "start_time": "06:00", "end_time": "09:00"},
            {"minimum_quantity": 3, "start_time": "18:00", "end_time": "21:00"},
        ],
    )
    standard = factory.event(type="standard")
    factory.reward(challenge, type="badge", tier=1)
    factory.reward(challenge, type="wallet", tier=2)
    user = factory.user()
    assert _enroll(client, challenge, user).status_code == 200

    anon = _my_event(client, factory)
    item = anon[str(challenge)]
    assert item["challenge"]["challenge_type"] == "booking"
    assert item["challenge"]["mode"] == "time_based"
    assert len(item["challenge"]["conditions"]) == 2
    assert item["challenge"]["target"] == 5
    assert [(r["type"], r["tier"]) for r in item["rewards"]] == [("badge", 1), ("wallet", 2)]
    assert set(item["rewards"][0]) == {"name", "type", "tier"}
    assert item["is_enrolled"] is None
    assert anon[str(standard)]["challenge"] is None
    assert anon[str(standard)]["rewards"] == []

    mine = _my_event(client, factory, user)
    assert mine[str(challenge)]["is_enrolled"] is True
    assert mine[str(standard)]["is_enrolled"] is False
    other = _my_event(client, factory, factory.user())
    assert other[str(challenge)]["is_enrolled"] is False

    # An invalid token keeps the endpoint public (treated as anonymous).
    r = client.get(
        "/api/v1/client/events",
        params={"search": factory.tag},
        headers={"Authorization": "Bearer not-a-token"},
    )
    assert r.status_code == 200
    assert all(i["is_enrolled"] is None for i in r.json()["data"])


def test_list_events_query_count_is_constant(app, session_factory, factory):
    """No N+1: building N items costs the same number of queries as 1 item."""
    from sqlalchemy import event as sa_event

    from app.models.event_event import EventEvent

    ids = [factory.event() for _ in range(4)]
    for eid in ids:
        factory.reward(eid)
    user = factory.user()

    def count_queries(event_ids):
        with session_factory() as s:
            events = s.query(EventEvent).filter(EventEvent.id.in_(event_ids)).all()
            n = {"q": 0}

            def before(*_a, **_k):
                n["q"] += 1

            engine = s.get_bind()
            sa_event.listen(engine, "before_cursor_execute", before)
            try:
                EventsService.events_to_responses(s, events, user_id=user["id"])
            finally:
                sa_event.remove(engine, "before_cursor_execute", before)
            return n["q"]

    assert count_queries(ids[:1]) == count_queries(ids)


# ------------------------------------------------------- 8. GET /events/me
def test_my_enrollments(client, factory, now):
    user = factory.user()
    other = factory.user()
    older = factory.event(conditions=[{"minimum_quantity": 4}, {"minimum_quantity": 6}])
    newer = factory.event(status="completed", finalized=True)
    win = factory.reward(newer, type="wallet", tier=1)
    factory.reward(newer, type="badge", tier=2)
    factory.enroll(older, user["id"], created_at=now() - timedelta(days=2), progress_count=3)
    factory.enroll(
        newer,
        user["id"],
        created_at=now() - timedelta(days=1),
        progress_count=7,
        eligible_at=now() - timedelta(hours=3),
        final_rank=1,
    )
    factory.grant(newer, user["id"], win, tier=1, status="granted")
    factory.enroll(newer, other["id"], progress_count=99)

    r = client.get("/api/v1/client/events/me", headers=user["auth"])
    assert r.status_code == 200, r.text
    body = r.json()
    rows = [d for d in body["data"] if d["event"]["id"] in {str(older), str(newer)}]
    assert [d["event"]["id"] for d in rows] == [str(newer), str(older)]  # newest first

    first, second = rows
    assert first["progress_count"] == 7
    assert first["eligible"] is True
    assert first["final_rank"] == 1
    assert first["event"]["status"] == "completed"
    assert first["event"]["is_enrolled"] is True
    assert first["rewards_won"] == [
        {"name": first["rewards_won"][0]["name"], "type": "wallet", "tier": 1, "status": "granted"}
    ]
    assert second["progress_count"] == 3
    assert second["target"] == 10
    assert second["eligible"] is False
    assert second["final_rank"] is None
    assert second["rewards_won"] == []
    # Members never see other participants.
    assert "participants" not in body and "leaderboard" not in body
    assert all(d["progress_count"] != 99 for d in rows)

    page = client.get("/api/v1/client/events/me", params={"page": 1, "limit": 1}, headers=user["auth"]).json()
    assert page["count"] == 1
    assert page["pagination"]["total"] == 2
    assert page["pagination"]["has_more"] is True


def test_my_enrollments_requires_auth(client):
    assert client.get("/api/v1/client/events/me").status_code in (401, 403)


# -------------------------------------------------- 9. DELETE /enroll (leave)
def test_leave_challenge(client, factory):
    event_id = factory.event()
    user = factory.user()
    url = f"/api/v1/client/events/{event_id}/enroll"
    assert client.delete(url, headers=user["auth"]).status_code == 404
    assert _enroll(client, event_id, user).status_code == 200
    r = client.delete(url, headers=user["auth"])
    assert r.status_code == 200, r.text
    assert client.delete(url, headers=user["auth"]).status_code == 404
    assert _my_event(client, factory, user)[str(event_id)]["is_enrolled"] is False
    # can join again afterwards
    assert _enroll(client, event_id, user).status_code == 200


def test_cannot_leave_finished_challenge(client, factory, now):
    user = factory.user()
    finalized = factory.event(status="completed", finalized=True)
    ended = factory.event(starts_at=now() - timedelta(days=3), ends_at=now() - timedelta(minutes=1))
    for event_id in (finalized, ended):
        factory.enroll(event_id, user["id"])
        r = client.delete(f"/api/v1/client/events/{event_id}/enroll", headers=user["auth"])
        assert r.status_code == 400, r.text


# ------------------------------------------------------------ GET /me/badges
def test_my_badges(client, factory, now):
    user = factory.user()
    event_id = factory.event(status="completed", finalized=True)
    b1 = factory.badge(icon_path="badges/hero.png")
    b2 = factory.badge()
    factory.award_badge(user["id"], b1, event_id, awarded_at=now() - timedelta(days=1))
    factory.award_badge(user["id"], b2, None, awarded_at=now())
    factory.award_badge(factory.user()["id"], b1, event_id)

    r = client.get("/api/v1/client/me/badges", headers=user["auth"])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert [d["badge_id"] for d in data] == [str(b2), str(b1)]
    assert data[1]["icon_url"] == "badges/hero.png"
    assert data[1]["event_id"] == str(event_id)
    assert data[1]["event_name"].startswith(factory.tag)
    assert data[0]["event_name"] is None
