"""Stripe Checkout gateway against the installed stripe-python, with Stripe's network calls faked.

Responses are real Stripe objects (construct_from), so this catches library changes
such as objects no longer being dicts.
"""
from __future__ import annotations

import pytest

stripe = pytest.importorskip("stripe")

from app.payments.base import PaymentRequest, PaymentStatus  # noqa: E402
from app.payments.stripe_gateway import StripePaymentGateway, _minor_units

SETTINGS = {"secret_key": "sk_test_x", "webhook_secret": "whsec_x", "callback_base_url": "https://api.example.com"}
SESSION = {
    "id": "cs_test_1",
    "object": "checkout.session",
    "url": "https://checkout.stripe.com/c/pay/cs_test_1",
    "client_reference_id": "order-1",
    "amount_total": 1999,
    "currency": "usd",
    "payment_status": "paid",
    "payment_intent": "pi_1",
}


def stripe_object(cls, data):
    return cls.construct_from(data, "sk_test_x")


@pytest.fixture()
def gateway():
    return StripePaymentGateway(dict(SETTINGS))


def test_checkout_session_has_no_payment_method_types_and_exact_cents(gateway, monkeypatch):
    sent = {}

    def create(**params):
        sent.update(params)
        return stripe_object(stripe.checkout.Session, SESSION)

    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(create))
    resp = gateway.create_payment(PaymentRequest(
        amount=19.99, currency="USD", order_id="order-1", customer_email="a@example.com", customer_name="A",
    ))

    assert "payment_method_types" not in sent
    assert sent["line_items"][0]["price_data"]["unit_amount"] == 1999
    assert resp.success and resp.payment_url == SESSION["url"] and resp.transaction_id == "cs_test_1"
    assert resp.raw_response["id"] == "cs_test_1"


def test_a_stripe_error_is_returned_not_raised(gateway, monkeypatch):
    def create(**params):
        raise stripe.error.InvalidRequestError("bad request", param="x")

    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(create))
    resp = gateway.create_payment(PaymentRequest(
        amount=10, currency="USD", order_id="o", customer_email="a@example.com", customer_name="A",
    ))
    assert not resp.success and "bad request" in resp.error_message


def _event(monkeypatch, event_type, **session):
    event = stripe_object(stripe.Event, {
        "id": "evt_1", "object": "event", "type": event_type,
        "data": {"object": {**SESSION, **session}},
    })
    monkeypatch.setattr(stripe.Webhook, "construct_event", staticmethod(lambda *a, **k: event))
    return {"raw_body": b"{}", "stripe_signature": "t=1,v1=x"}


@pytest.mark.parametrize("event_type, payment_status, expected", [
    ("checkout.session.completed", "paid", PaymentStatus.SUCCESS),
    ("checkout.session.completed", "unpaid", PaymentStatus.PENDING),       # bank debit still settling
    ("checkout.session.async_payment_succeeded", "paid", PaymentStatus.SUCCESS),
    ("checkout.session.async_payment_failed", "unpaid", PaymentStatus.FAILED),
])
def test_webhook_session_events(gateway, monkeypatch, event_type, payment_status, expected):
    result = gateway.handle_callback(_event(monkeypatch, event_type, payment_status=payment_status))
    assert result.success and result.status == expected
    assert (result.order_id, result.transaction_id, result.amount, result.currency) == ("order-1", "cs_test_1", 19.99, "USD")


def test_other_webhook_events_are_ignored(gateway, monkeypatch):
    result = gateway.handle_callback(_event(monkeypatch, "payment_intent.created"))
    assert result.success and result.status == PaymentStatus.PENDING and result.order_id == ""


def test_verify_payment_reads_the_session(gateway, monkeypatch):
    monkeypatch.setattr(stripe.checkout.Session, "retrieve",
                        staticmethod(lambda sid: stripe_object(stripe.checkout.Session, SESSION)))
    result = gateway.verify_payment("cs_test_1")
    assert result.success and result.status == PaymentStatus.SUCCESS
    assert (result.order_id, result.amount, result.currency) == ("order-1", 19.99, "USD")


def test_refund_uses_the_sessions_payment_intent(gateway, monkeypatch):
    sent = {}
    monkeypatch.setattr(stripe.checkout.Session, "retrieve",
                        staticmethod(lambda sid: stripe_object(stripe.checkout.Session, SESSION)))

    def refund(**params):
        sent.update(params)
        return stripe_object(stripe.Refund, {"id": "re_1", "object": "refund", "amount": params["amount"]})

    monkeypatch.setattr(stripe.Refund, "create", staticmethod(refund))
    resp = gateway.refund_payment("cs_test_1", 5.10)
    assert (sent["payment_intent"], sent["amount"]) == ("pi_1", 510)
    assert resp.success and resp.refund_id == "re_1" and resp.amount == 5.10


def test_minor_units_rounding():
    assert [_minor_units(v) for v in (19.99, 0.29, 10, "12.345")] == [1999, 29, 1000, 1235]
