"""A Stripe webhook for a wallet top-up credits the wallet and writes no package record."""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.payments import routes
from app.payments.base import CallbackResult, GatewayType, PaymentStatus

from tests.conftest import TENANT_ID


class FakeGateway:
    def __init__(self, result):
        self.result = result

    def handle_callback(self, payload):
        return self.result


@pytest.fixture()
def topup(factory, session_factory):
    """A member who started a 100.00 top-up: only the initiation row exists, as in add_wallet_balance."""
    member = factory.user()
    order_id, session_id = str(uuid.uuid4()), f"cs_test_{uuid.uuid4().hex}"
    with session_factory() as s:
        s.execute(text("update users set wallet = 50 where id = :id"), {"id": member["id"]})
        s.execute(
            text(
                "insert into sales_transactions (tenant_id, payment_method, gateway, gateway_txn_id, source, status, "
                "amount, currency, user_id, created_by_type, created_by_id, extra_metadata, txn_ref) values "
                "(:t, 'gateway', 'stripe', :sid, 'wallet', 'pending', 100, 'INR', :u, 'member', :u, "
                "cast(:meta as jsonb), :ref)"
            ),
            {"t": TENANT_ID, "sid": session_id, "u": member["id"], "ref": f"TXN-{uuid.uuid4().hex[:18]}",
             "meta": '{"event": "created", "client_order_id": "%s", "purpose": "wallet_add"}' % order_id},
        )
        s.commit()
    yield {"member": member, "order_id": order_id, "session_id": session_id}
    with session_factory() as s:
        s.execute(text("delete from sales_transactions where gateway_txn_id = :sid"), {"sid": session_id})
        wallet_ids = [r[0] for r in s.execute(text("select id from wallet_transactions where transaction_id = :sid"), {"sid": session_id})]
        for wid in wallet_ids:
            s.execute(text("delete from sales where wallet_transaction_id = :w"), {"w": wid})
        s.execute(text("delete from wallet_transactions where transaction_id = :sid"), {"sid": session_id})
        s.commit()


def deliver(monkeypatch, session_factory, topup, status=PaymentStatus.SUCCESS):
    result = CallbackResult(
        success=True, order_id=topup["order_id"], transaction_id=topup["session_id"], status=status,
        gateway=GatewayType.STRIPE, amount=100.0, currency="INR", raw_payload={},
    )
    monkeypatch.setattr(routes, "get_gateway", lambda tenant_id, gateway_type: FakeGateway(result))
    async def no_event(*a, **k):
        return None
    monkeypatch.setattr(routes.WalletNotificationService, "publish_topup_success", no_event)
    with session_factory() as db:
        return asyncio.run(routes._handle_payment_callback(
            gateway_type="stripe", request=None, db=db, tenant_id=TENANT_ID, payload={},
        ))


def state(session_factory, topup):
    with session_factory() as s:
        wallet = s.execute(text("select wallet from users where id = :id"), {"id": topup["member"]["id"]}).scalar()
        rows = s.execute(
            text("select source, status::text, user_id from sales_transactions where gateway_txn_id = :sid"),
            {"sid": topup["session_id"]},
        ).all()
    return Decimal(str(wallet)), rows


def test_webhook_credits_the_wallet_and_writes_no_package_row(monkeypatch, session_factory, topup):
    out = deliver(monkeypatch, session_factory, topup)
    assert out["success"] is True
    wallet, rows = state(session_factory, topup)
    assert wallet == Decimal("150")
    assert [r[0] for r in rows] == ["wallet"]          # no extra source='package' row
    assert rows[0][1] == "success" and rows[0][2] is not None


def test_redelivered_webhook_does_not_credit_twice(monkeypatch, session_factory, topup):
    deliver(monkeypatch, session_factory, topup)
    deliver(monkeypatch, session_factory, topup)       # Stripe retries and repeats events
    wallet, rows = state(session_factory, topup)
    assert wallet == Decimal("150") and len(rows) == 1


def test_a_pending_session_credits_nothing(monkeypatch, session_factory, topup):
    deliver(monkeypatch, session_factory, topup, status=PaymentStatus.PENDING)
    wallet, rows = state(session_factory, topup)
    assert wallet == Decimal("50")
    assert [r[0] for r in rows] == ["wallet"]
