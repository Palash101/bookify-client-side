"""
Member booking rules (fitnez_main_backend/docs/booking-rules.md):

§1 package eligibility by feature_type (bookings, waitlist joins, /validate checks)
§2 member payments: paid class is wallet-only (cash / gateway rejected)
§3 waitlist: charged at join, linked wallet txns, full refund on cancel, no double
   charge on promotion, queue order + renumbering
plus the enable_class_package=false message.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from tests.conftest import TENANT_ID

TZ_NAME = "Asia/Dubai"
TZ = ZoneInfo(TZ_NAME)

MSG_LOCATION = "This package can't be used at this location"
MSG_PROGRAM = "This package can't be used for this training program"
MSG_EVENT = "This package can only be used for its event"
MSG_TIME = "This package can only be used for classes starting between 06:00 and 11:00"
MSG_CASH = "Cash payment is only available at the front desk"
MSG_GATEWAY = "Card payment for single classes is not available yet — pay with your wallet"
MSG_PKG_DISABLED = "Package booking is disabled"

TIME_WINDOW = {"time_restriction": {"start_time": "06:00", "end_time": "11:00"}}


# --------------------------------------------------------------------------- config
def _config(**overrides: Any):
    from app.schemas.gym_config_value import GymConfigValue

    raw: Dict[str, Any] = {
        "organization_config": {"timezone": TZ_NAME, "currency": "AED"},
        "payment_pricing": {
            "enable_free_classes": True,
            "enable_class_package": True,
            "enable_pay_per_class": True,
        },
        "booking_settings": {
            "allow_waiting_list": True,
            "auto_confirm_booking": True,
            "allow_late_cancellations": True,
            "cancellation_window_hours": 0,
            "advance_booking_window_days": 0,
            "booking_cutoff_minutes": 0,
        },
    }
    for dotted, value in overrides.items():
        section, key = dotted.split("__", 1)
        raw[section][key] = value
    return GymConfigValue.model_validate(raw)


@pytest.fixture()
def gym(app, monkeypatch):
    """Per-test gym_config (bypasses the tenant settings row). Call gym(**overrides)."""
    from app.dependencies import get_gym_config_for_active_user
    from app.services.gym_config_service import GymConfigService

    state = {"cfg": _config()}

    def set_config(**overrides: Any):
        state["cfg"] = _config(**overrides)
        return state["cfg"]

    app.dependency_overrides[get_gym_config_for_active_user] = lambda: state["cfg"]
    monkeypatch.setattr(GymConfigService, "get_gym_config", staticmethod(lambda db, tenant_id: state["cfg"]))
    yield set_config
    app.dependency_overrides.pop(get_gym_config_for_active_user, None)


# -------------------------------------------------------------------------- factory
class BookingFactory:
    def __init__(self, base):
        self.base = base
        self.sf = base.sf
        self.tag = base.tag
        self.locations: list = []
        self.programs: list = []
        self.classes: list = []
        self.packages: list = []
        self.sales: list = []
        self.user_packages: list = []
        self._trainer: Optional[Dict[str, Any]] = None

    def _exec(self, sql: str, **params: Any):
        with self.sf() as s:
            res = s.execute(text(sql), params)
            s.commit()
            return res

    def _scalar(self, sql: str, **params: Any):
        with self.sf() as s:
            return s.execute(text(sql), params).scalar()

    # ------------------------------------------------------------------ setup rows
    def member(self, wallet: Decimal = Decimal("0")) -> Dict[str, Any]:
        user = self.base.user()
        self._exec("update users set wallet = :w where id = :id", w=wallet, id=user["id"])
        return user

    def location(self) -> uuid.UUID:
        lid = uuid.uuid4()
        self._exec(
            "insert into locations (id, tenant_id, name, address_line1, city, country, is_active) "
            "values (:id, :t, :n, 'x', 'Dubai', 'AE', true)",
            id=lid, t=TENANT_ID, n=f"{self.tag} loc {len(self.locations)}",
        )
        self.locations.append(lid)
        return lid

    def program(self, location_id: Optional[uuid.UUID]) -> int:
        with self.sf() as s:
            pid = s.execute(
                text("insert into training_programs (tenant_id, location_id, name, training_mode) "
                     "values (:t, :l, :n, 'one_to_many') returning id"),
                {"t": TENANT_ID, "l": location_id, "n": f"{self.tag} prog {len(self.programs)}"},
            ).scalar()
            s.commit()
        self.programs.append(pid)
        return pid

    def gym_class(
        self,
        *,
        program_id: int,
        start: str = "07:00",
        booking_type: str = "packages",
        price: Optional[Decimal] = None,
        max_bookings: int = 10,
        max_waitings: int = 5,
        days_ahead: int = 2,
    ) -> uuid.UUID:
        if self._trainer is None:
            self._trainer = self.base.user()
        cid = uuid.uuid4()
        class_date = (datetime.now(TZ) + timedelta(days=days_ahead)).date()
        h, m = (int(x) for x in start.split(":"))
        end_h = (h + 1) % 24
        self._exec(
            "insert into classes (id, title, trainer_id, class_date, start_time, end_time, "
            "booking_type, gender, status, max_bookings, max_waitings, price, training_programme_id) "
            "values (:id, :title, :tr, :d, make_time(:h, :m, 0), make_time(:eh, :m, 0), "
            "cast(:bt as booking_type_enum), 'mixed', 'published', :mb, :mw, :price, :pid)",
            id=cid, title=f"{self.tag} class", tr=self._trainer["id"], d=class_date, h=h, m=m, eh=end_h,
            bt=booking_type, mb=max_bookings, mw=max_waitings, price=price, pid=program_id,
        )
        self.classes.append(cid)
        return cid

    def package(self, user_id, *, feature: str = "normal", restriction: Optional[dict] = None,
                sessions: int = 10) -> uuid.UUID:
        """Package + paid sale + user_packages entitlement. Returns user_packages.id."""
        pkg = uuid.uuid4()
        self._exec(
            "insert into packages (id, name, package_type, tenant_id, feature_type, booking_restriction, "
            "is_private, status, validity_start) values (:id, :n, 'one_time', :t, cast(:f as feature_type_enum), "
            "cast(:r as jsonb), false, 'active', current_date)",
            id=pkg, n=f"{self.tag} pkg {feature}", t=TENANT_ID, f=feature,
            r=json.dumps(restriction) if restriction is not None else None,
        )
        self.packages.append(pkg)
        sale = uuid.uuid4()
        self._exec(
            "insert into sales (id, tenant_id, user_id, amount, item_type, item_id, payment_source) "
            "values (:id, :t, :u, 100, 'package', :p, 'package_wallet')",
            id=sale, t=TENANT_ID, u=user_id, p=pkg,
        )
        self.sales.append(sale)
        up = uuid.uuid4()
        self._exec(
            "insert into user_packages (id, user_id, package_id, sale_id, expire_at, session_count, total_session) "
            "values (:id, :u, :p, :s, :e, :n, :n)",
            id=up, u=user_id, p=pkg, s=sale, e=datetime.now(timezone.utc) + timedelta(days=30), n=sessions,
        )
        self.user_packages.append(up)
        return up

    # ------------------------------------------------------------------- reads
    def sessions_left(self, up_id) -> int:
        return int(self._scalar("select session_count from user_packages where id = :id", id=up_id))

    def wallet(self, user_id) -> Decimal:
        return Decimal(str(self._scalar("select wallet from users where id = :id", id=user_id)))

    def booking(self, booking_id) -> Dict[str, Any]:
        with self.sf() as s:
            row = s.execute(text("select * from class_bookings where id = :id"), {"id": booking_id}).mappings().first()
            return dict(row) if row else {}

    def bookings_for(self, class_id, user_id) -> list:
        with self.sf() as s:
            return [dict(r) for r in s.execute(
                text("select * from class_bookings where class_id = :c and user_id = :u"),
                {"c": class_id, "u": user_id},
            ).mappings()]

    def wallet_txns(self, user_id) -> list:
        with self.sf() as s:
            return [dict(r) for r in s.execute(
                text("select * from wallet_transactions where user_id = :u order by created_at"), {"u": user_id}
            ).mappings()]

    def cleanup(self) -> None:
        users = list(self.base.users)
        with self.sf() as s:
            if self.classes:
                s.execute(text("delete from user_package_tracking where reference_id in "
                               "(select id from class_bookings where class_id = any(:ids))"), {"ids": self.classes})
                s.execute(text("delete from class_bookings where class_id = any(:ids)"), {"ids": self.classes})
                s.execute(text("delete from classes where id = any(:ids)"), {"ids": self.classes})
            if self.user_packages:
                s.execute(text("delete from user_package_tracking where user_package_id = any(:ids)"),
                          {"ids": self.user_packages})
                s.execute(text("delete from user_packages where id = any(:ids)"), {"ids": self.user_packages})
            if self.sales:
                s.execute(text("delete from sales where id = any(:ids)"), {"ids": self.sales})
            if self.packages:
                s.execute(text("delete from packages where id = any(:ids)"), {"ids": self.packages})
            if users:
                s.execute(text("delete from wallet_transactions where user_id = any(:ids)"), {"ids": users})
            if self.programs:
                s.execute(text("delete from training_programs where id = any(:ids)"), {"ids": self.programs})
            if self.locations:
                s.execute(text("delete from locations where id = any(:ids)"), {"ids": self.locations})
            s.commit()


@pytest.fixture()
def bf(factory, gym):
    f = BookingFactory(factory)
    try:
        yield f
    finally:
        f.cleanup()


# ------------------------------------------------------------------------- helpers
def _body(mode: str, up_id=None) -> dict:
    body: Dict[str, Any] = {"payment_mode": mode}
    if up_id is not None:
        body["user_package_purchase_id"] = str(up_id)
    return body


def _book(client, class_id, user, mode, up_id=None, waiting=False):
    path = f"/api/v1/client/classes/{class_id}/bookings" + ("/waiting" if waiting else "")
    return client.post(path, json=_body(mode, up_id), headers=user["auth"])


def _validate(client, class_id, user, mode, up_id=None):
    r = client.post(f"/api/v1/client/classes/{class_id}/bookings/validate",
                    json=_body(mode, up_id), headers=user["auth"])
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _cancel(client, class_id, booking_id, user):
    return client.post(f"/api/v1/client/classes/{class_id}/bookings/{booking_id}/cancel",
                       json={"reason": "pytest"}, headers=user["auth"])


# ============================================================ §1 package eligibility
CASES = [
    # id, feature, restriction builder(main, marina, progA, progB), class start, class program key, ok, check, msg
    ("loc-ok", "location", lambda L: {"location_restriction": {"location_ids": [str(L["main"])]}}, "07:00", "progMain", True, "package_location", None),
    ("loc-wrong", "location", lambda L: {"location_restriction": {"location_ids": [str(L["marina"])]}}, "07:00", "progMain", False, "package_location", MSG_LOCATION),
    ("loc-legacy-key", "location", lambda L: {"location": {"location_ids": [str(L["marina"])]}}, "07:00", "progMain", False, "package_location", MSG_LOCATION),
    ("loc-class-without-location", "location", lambda L: {"location_restriction": {"location_ids": [str(L["main"])]}}, "07:00", "progNoLoc", False, "package_location", MSG_LOCATION),
    ("time-07:00", "time_based", lambda L: TIME_WINDOW, "07:00", "progMain", True, "package_time_slot", None),
    ("time-06:00-inclusive", "time_based", lambda L: TIME_WINDOW, "06:00", "progMain", True, "package_time_slot", None),
    ("time-05:59", "time_based", lambda L: TIME_WINDOW, "05:59", "progMain", False, "package_time_slot", MSG_TIME),
    ("time-11:00-exclusive", "time_based", lambda L: TIME_WINDOW, "11:00", "progMain", False, "package_time_slot", MSG_TIME),
    ("time-18:00", "time_based", lambda L: TIME_WINDOW, "18:00", "progMain", False, "package_time_slot", MSG_TIME),
    ("prog-ok", "program_based", lambda L: {"program_restriction": {"training_program_ids": [L["progMain"]]}}, "07:00", "progMain", True, "package_program", None),
    ("prog-wrong", "program_based", lambda L: {"program_restriction": {"training_program_ids": [L["progMarina"]]}}, "07:00", "progMain", False, "package_program", MSG_PROGRAM),
    ("event", "event_based", lambda L: {"event_restriction": {"event_ids": [str(uuid.uuid4())]}}, "07:00", "progMain", False, "package_event", MSG_EVENT),
    # Only the block matching feature_type counts: a stale block on a normal package is ignored.
    ("normal-ignores-blocks", "normal", lambda L: {"location_restriction": {"location_ids": [str(L["marina"])]}}, "07:00", "progMain", True, "package_location", None),
    # feature_type without its block = unrestricted.
    ("time-without-block", "time_based", lambda L: None, "05:00", "progMain", True, "package_time_slot", None),
]


@pytest.fixture()
def places(bf):
    main, marina = bf.location(), bf.location()
    return {
        "main": main,
        "marina": marina,
        "progMain": bf.program(main),
        "progMarina": bf.program(marina),
        "progNoLoc": bf.program(None),
    }


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_package_eligibility_matrix(client, bf, places, case):
    _id, feature, restriction, start, prog_key, ok, check, msg = case
    member = bf.member()
    up = bf.package(member["id"], feature=feature, restriction=restriction(places))
    class_id = bf.gym_class(program_id=places[prog_key], start=start)

    v = _validate(client, class_id, member, "package", up)
    assert v["checks"][check]["pass"] is ok, v["checks"]
    assert "one_time_package_reuse" not in v["checks"]
    if ok:
        assert v["valid"] is True, v
    else:
        assert v["valid"] is False
        assert v["checks"][check]["message"] == msg
        assert v["message"] == msg
        assert v["proceed_to"] == "payment_selection"

    before = bf.sessions_left(up)
    r = _book(client, class_id, member, "package", up)
    if ok:
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "confirmed"
        assert bf.sessions_left(up) == before - 1
    else:
        assert r.status_code == 400, r.text
        assert r.json()["message"] == msg
        assert bf.sessions_left(up) == before
        assert bf.bookings_for(class_id, member["id"]) == []


def test_restricted_package_cannot_join_waitlist(client, bf, places):
    holder = bf.member()
    class_id = bf.gym_class(program_id=places["progMain"], start="18:00", max_bookings=1)
    assert _book(client, class_id, holder, "package", bf.package(holder["id"])).status_code == 200

    member = bf.member()
    up = bf.package(member["id"], feature="time_based", restriction=TIME_WINDOW)
    r = _book(client, class_id, member, "package", up, waiting=True)
    assert r.status_code == 400
    assert r.json()["message"] == MSG_TIME
    assert bf.sessions_left(up) == 10


def test_package_booking_disabled_message(client, bf, gym, places):
    gym(payment_pricing__enable_class_package=False)
    member = bf.member()
    up = bf.package(member["id"], feature="program_based",
                    restriction={"program_restriction": {"training_program_ids": [places["progMain"]]}})
    class_id = bf.gym_class(program_id=places["progMain"])

    v = _validate(client, class_id, member, "package", up)
    assert v["valid"] is False
    assert v["message"] == MSG_PKG_DISABLED
    assert v["checks"]["package_valid"]["message"] == MSG_PKG_DISABLED

    r = _book(client, class_id, member, "package", up)
    assert r.status_code == 400
    assert r.json()["message"] == MSG_PKG_DISABLED
    assert bf.sessions_left(up) == 10


# ======================================================================= §2 payments
@pytest.mark.parametrize("mode,msg", [("cash", MSG_CASH), ("gateway", MSG_GATEWAY)])
def test_member_paid_class_rejects_cash_and_gateway(client, bf, places, mode, msg):
    member = bf.member(wallet=Decimal("100"))
    class_id = bf.gym_class(program_id=places["progMain"], booking_type="price", price=Decimal("15"))

    v = _validate(client, class_id, member, mode)
    assert v["valid"] is False
    assert v["checks"]["class_payment_mode"] == {"pass": False, "message": msg, "allowed": ["wallet"]}
    assert v["message"] == msg

    r = _book(client, class_id, member, mode)
    assert r.status_code == 400
    assert r.json()["message"] == msg
    assert bf.bookings_for(class_id, member["id"]) == []
    assert bf.wallet(member["id"]) == Decimal("100")


def test_member_paid_class_wallet_debit_is_linked(client, bf, places):
    member = bf.member(wallet=Decimal("100"))
    class_id = bf.gym_class(program_id=places["progMain"], booking_type="price", price=Decimal("15"))
    r = _book(client, class_id, member, "wallet")
    assert r.status_code == 200, r.text
    booking_id = uuid.UUID(r.json()["data"]["booking_id"])
    (txn,) = bf.wallet_txns(member["id"])
    assert (txn["direction"], txn["amount"]) == ("debit", Decimal("15.00"))
    assert (txn["reference_type"], txn["reference_id"]) == ("class_booking", booking_id)
    assert bf.wallet(member["id"]) == Decimal("85")


# ======================================================================= §3 waitlist
def _full_paid_class(client, bf, places, **kw):
    holder = bf.member(wallet=Decimal("100"))
    class_id = bf.gym_class(program_id=places["progMain"], booking_type="price", price=Decimal("15"),
                            max_bookings=1, **kw)
    r = _book(client, class_id, holder, "wallet")
    assert r.status_code == 200, r.text
    return class_id, holder, uuid.UUID(r.json()["data"]["booking_id"])


def test_wallet_waitlist_charged_at_join_and_refunded_in_full_outside_window(client, bf, gym, places):
    class_id, _holder, _ = _full_paid_class(client, bf, places)
    # Strict gym: no late cancellations, 1000h window -> we are outside the window.
    gym(booking_settings__allow_late_cancellations=False, booking_settings__cancellation_window_hours=1000)

    member = bf.member(wallet=Decimal("155"))
    r = _book(client, class_id, member, "wallet", waiting=True)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert (data["status"], data["waiting_position"]) == ("waiting", 1)
    booking_id = uuid.UUID(data["booking_id"])
    assert bf.wallet(member["id"]) == Decimal("140")
    (debit,) = bf.wallet_txns(member["id"])
    assert (debit["reference_type"], debit["reference_id"]) == ("class_booking", booking_id)

    r = _cancel(client, class_id, booking_id, member)
    assert r.status_code == 200, r.text
    assert bf.wallet(member["id"]) == Decimal("155")
    debit, credit = bf.wallet_txns(member["id"])
    assert (credit["direction"], credit["amount"]) == ("credit", Decimal("15.00"))
    assert (credit["reference_type"], credit["reference_id"]) == ("class_booking", booking_id)


def test_package_waitlist_refunded_in_full_outside_window(client, bf, gym, places):
    holder = bf.member()
    class_id = bf.gym_class(program_id=places["progMain"], max_bookings=1)
    assert _book(client, class_id, holder, "package", bf.package(holder["id"])).status_code == 200
    gym(booking_settings__allow_late_cancellations=True, booking_settings__cancellation_window_hours=1000)

    member = bf.member()
    up = bf.package(member["id"])
    r = _book(client, class_id, member, "package", up, waiting=True)
    assert r.status_code == 200, r.text
    assert bf.sessions_left(up) == 9
    r = _cancel(client, class_id, r.json()["data"]["booking_id"], member)
    assert r.status_code == 200, r.text
    assert bf.sessions_left(up) == 10


def test_confirmed_wallet_booking_late_cancel_keeps_money(client, bf, gym, places):
    """Control: outside the window a confirmed (not waitlisted) booking is not refunded."""
    gym(booking_settings__allow_late_cancellations=True, booking_settings__cancellation_window_hours=1000)
    class_id, holder, booking_id = _full_paid_class(client, bf, places)
    assert _cancel(client, class_id, booking_id, holder).status_code == 200
    assert bf.wallet(holder["id"]) == Decimal("85")


def test_promotion_does_not_charge_again(client, bf, places):
    class_id, holder, holder_booking = _full_paid_class(client, bf, places)
    member = bf.member(wallet=Decimal("50"))
    r = _book(client, class_id, member, "wallet", waiting=True)
    assert r.status_code == 200, r.text
    waiting_id = uuid.UUID(r.json()["data"]["booking_id"])
    assert bf.wallet(member["id"]) == Decimal("35")

    assert _cancel(client, class_id, holder_booking, holder).status_code == 200
    promoted = bf.booking(waiting_id)
    assert promoted["status"] == "confirmed"
    assert promoted["waiting_position"] is None
    assert bf.wallet(member["id"]) == Decimal("35")
    assert [t["direction"] for t in bf.wallet_txns(member["id"])] == ["debit"]


def test_package_promotion_does_not_deduct_again(client, bf, places):
    holder = bf.member()
    class_id = bf.gym_class(program_id=places["progMain"], max_bookings=1)
    r = _book(client, class_id, holder, "package", bf.package(holder["id"]))
    holder_booking = r.json()["data"]["booking_id"]

    member = bf.member()
    up = bf.package(member["id"])
    r = _book(client, class_id, member, "package", up, waiting=True)
    waiting_id = r.json()["data"]["booking_id"]
    assert bf.sessions_left(up) == 9

    assert _cancel(client, class_id, holder_booking, holder).status_code == 200
    assert bf.booking(waiting_id)["status"] == "confirmed"
    assert bf.sessions_left(up) == 9


def test_promotion_charges_legacy_uncharged_wallet_entry_once(client, bf, places):
    class_id, holder, holder_booking = _full_paid_class(client, bf, places)
    member = bf.member(wallet=Decimal("40"))
    legacy_id = uuid.uuid4()
    bf._exec(
        "insert into class_bookings (id, tenant_id, user_id, class_id, status, waiting_position, booked_at, "
        "payment_mode, sessions_deducted, booking_ref) values (:id, :t, :u, :c, 'waiting', 1, now(), 'wallet', 0, :ref)",
        id=legacy_id, t=TENANT_ID, u=member["id"], c=class_id, ref=f"BK-PY{uuid.uuid4().hex[:10]}",
    )
    assert _cancel(client, class_id, holder_booking, holder).status_code == 200
    assert bf.booking(legacy_id)["status"] == "confirmed"
    assert bf.wallet(member["id"]) == Decimal("25")
    (debit,) = bf.wallet_txns(member["id"])
    assert (debit["reference_type"], debit["reference_id"]) == ("class_booking", legacy_id)


def test_promotion_follows_waiting_position_and_renumbers(client, bf, places):
    class_id, holder, holder_booking = _full_paid_class(client, bf, places, max_waitings=5)
    a, b, c = (bf.member(wallet=Decimal("50")) for _ in range(3))
    ids = []
    for m in (a, b, c):
        r = _book(client, class_id, m, "wallet", waiting=True)
        assert r.status_code == 200, r.text
        ids.append(uuid.UUID(r.json()["data"]["booking_id"]))
    assert [bf.booking(i)["waiting_position"] for i in ids] == [1, 2, 3]

    # b leaves the waitlist -> c moves up to 2.
    assert _cancel(client, class_id, ids[1], b).status_code == 200
    assert bf.booking(ids[2])["waiting_position"] == 2

    # Queue order is waiting_position, not booked_at: put c first.
    bf._exec("update class_bookings set waiting_position = 1 where id = :id", id=ids[2])
    bf._exec("update class_bookings set waiting_position = 2 where id = :id", id=ids[0])
    assert _cancel(client, class_id, holder_booking, holder).status_code == 200
    assert bf.booking(ids[2])["status"] == "confirmed"
    remaining = bf.booking(ids[0])
    assert (remaining["status"], remaining["waiting_position"]) == ("waiting", 1)
