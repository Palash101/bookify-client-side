"""Trainer assignment on the member API: list, details, validate/book, my trainers.

Modes (gym_config.booking_settings.trainer_assignment_mode): open, assigned_only, mixed.
Needs the client_trainers table / classes.audience / users.can_book_online
(fitnez_main_backend tasks/sql/tenant.sql).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from tests.conftest import TENANT_ID
from tests.test_booking_rules import TZ, BookingFactory, _book, _validate, bf, gym  # noqa: F401

API = "/api/v1/client"


@pytest.fixture()
def setup(bf):
    loc = bf.location()
    prog = bf.program(loc)
    asha, ravi = bf.base.user(), bf.base.user()
    member = bf.member()
    bf._exec("update users set first_name = 'Asha', last_name = 'K' where id = :id", id=asha["id"])
    bf._exec("update users set first_name = 'Ravi', last_name = 'M' where id = :id", id=ravi["id"])
    return {"loc": loc, "prog": prog, "asha": asha, "ravi": ravi, "member": member, "bf": bf}


def make_class(s, *trainers, audience="everyone", start="08:00"):
    bf = s["bf"]
    cid = uuid.uuid4()
    day = (datetime.now(TZ) + timedelta(days=2)).date()
    h = int(start.split(":")[0])
    ids = [str(t["id"]) for t in trainers]
    bf._exec(
        "insert into classes (id, title, tenant_id, trainer_id, trainer_ids, class_date, start_time, end_time, "
        "booking_type, gender, status, max_bookings, max_waitings, training_programme_id, audience) "
        "values (:id, :title, :t, cast(:lead as uuid), cast(:ids as uuid[]), :d, make_time(:h, 0, 0), "
        "make_time(:eh, 0, 0), 'free', 'mixed', 'published', 10, 0, :pid, :aud)",
        id=cid, title=f"{bf.tag} {start}", t=TENANT_ID, lead=ids[0], ids="{" + ",".join(ids) + "}", d=day,
        h=h, eh=h + 1, pid=s["prog"], aud=audience,
    )
    bf.classes.append(cid)
    return cid, day


def assign(s, member, *trainers):
    for t in trainers:
        s["bf"]._exec(
            "insert into client_trainers (tenant_id, client_id, trainer_id) values (:t, :c, :tr)",
            t=TENANT_ID, c=member["id"], tr=t["id"],
        )


def listed(client, s, day, user=None):
    r = client.get(f"{API}/locations/{s['loc']}/classes?date={day}", headers=user["auth"] if user else None)
    assert r.status_code == 200, r.text
    return {c["id"] for c in r.json()["data"]}


def details(client, s, cid, user=None):
    r = client.get(f"{API}/locations/{s['loc']}/classes/{cid}", headers=user["auth"] if user else None)
    assert r.status_code == 200, r.text
    return r.json().get("data")  # "Class not found" has no data


def test_open_mode_shows_and_books_everything(client, gym, setup):
    s = setup
    a, day = make_class(s, s["asha"])
    assert listed(client, s, day, s["member"]) == {str(a)}
    assert _book(client, a, s["member"], "free").status_code in (200, 201)


def test_assigned_only_lists_and_books_own_trainers_classes(client, gym, setup):
    s = setup
    gym(booking_settings__trainer_assignment_mode="assigned_only")
    assign(s, s["member"], s["asha"])
    mine, day = make_class(s, s["asha"], start="08:00")
    co, _ = make_class(s, s["ravi"], s["asha"], start="10:00")
    theirs, _ = make_class(s, s["ravi"], start="12:00")

    assert listed(client, s, day, s["member"]) == {str(mine), str(co)}
    assert listed(client, s, day) == {str(mine), str(co), str(theirs)}  # visitors see the timetable
    assert details(client, s, theirs, s["member"]) is None
    assert details(client, s, mine, s["member"])["class_id"] == str(mine)

    result = _validate(client, theirs, s["member"], "free")
    assert result["valid"] is False
    assert result["checks"]["assigned_trainer"]["message"] == "This class is only for clients of Ravi M."
    r = _book(client, theirs, s["member"], "free")
    assert r.status_code == 400 and "only for clients of Ravi M" in r.text
    assert _book(client, mine, s["member"], "free").status_code in (200, 201)


def test_assigned_only_without_trainers_sees_nothing(client, gym, setup):
    s = setup
    gym(booking_settings__trainer_assignment_mode="assigned_only")
    _, day = make_class(s, s["asha"])
    assert listed(client, s, day, s["member"]) == set()


def test_mixed_mode(client, gym, setup):
    s = setup
    gym(booking_settings__trainer_assignment_mode="mixed")
    assign(s, s["member"], s["asha"])
    open_class, day = make_class(s, s["ravi"], start="08:00")
    mine, _ = make_class(s, s["asha"], audience="assigned_clients", start="10:00")
    theirs, _ = make_class(s, s["ravi"], audience="assigned_clients", start="12:00")

    assert listed(client, s, day, s["member"]) == {str(open_class), str(mine)}
    assert listed(client, s, day) == {str(open_class)}  # visitors: open classes only
    assert _validate(client, open_class, s["member"], "free")["valid"] is True
    assert _validate(client, theirs, s["member"], "free")["valid"] is False


def test_a_booked_class_stays_viewable(client, gym, setup):
    s = setup
    theirs, _ = make_class(s, s["ravi"])
    assert _book(client, theirs, s["member"], "free").status_code in (200, 201)
    gym(booking_settings__trainer_assignment_mode="assigned_only")  # e.g. staff booked them anyway
    assert details(client, s, theirs, s["member"])["class_id"] == str(theirs)


def test_cant_book_online(client, gym, setup):
    s = setup
    s["bf"]._exec("update users set can_book_online = false where id = :id", id=s["member"]["id"])
    a, _ = make_class(s, s["asha"])
    result = _validate(client, a, s["member"], "free")
    assert result["valid"] is False and "contact the gym" in result["checks"]["online_booking_allowed"]["message"]


def test_my_trainers(client, gym, setup):
    s = setup
    gym(booking_settings__trainer_assignment_mode="mixed")
    assign(s, s["member"], s["asha"])
    r = client.get(f"{API}/trainers/mine", headers=s["member"]["auth"])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["trainer_assignment_mode"] == "mixed" and data["can_book_online"] is True
    assert [t["name"] for t in data["trainers"]] == ["Asha K"]
