"""
Payment gateway factory tests (TenantPaymentSettings + get_gateway).
"""

from __future__ import annotations

import uuid

import pytest

from app.payments.base import GatewayType
from app.payments.factory import (
    GATEWAY_REGISTRY,
    TenantPaymentSettings,
    get_gateway,
)
from app.payments.stripe_gateway import StripePaymentGateway
from tests.conftest import TENANT_ID


class TestTenantPaymentSettingsLoader:
    def test_prefers_stripe_as_active_gateway(self, db, factory):
        factory.tenant_payment_settings(
            gateway_type="paypal",
            payment_config={
                "client_id": "cid",
                "client_secret": "sec",
                "callback_base_url": "https://api.test.example",
            },
        )
        factory.tenant_payment_settings(gateway_type="stripe")

        TenantPaymentSettings._cache.clear()
        settings = TenantPaymentSettings._load_from_db(TENANT_ID)

        assert settings["active_gateway"] == "stripe"
        assert "stripe" in settings["gateways"]
        assert "paypal" in settings["gateways"]

    def test_raises_when_no_rows(self, db):
        TenantPaymentSettings._cache.clear()
        with pytest.raises(ValueError, match="No payment settings configured"):
            TenantPaymentSettings._load_from_db("missing-tenant")

    def test_raises_when_configs_empty(self, db, factory):
        row = factory.tenant_payment_settings(gateway_type="stripe")
        row.payment_config = None
        db.flush()

        TenantPaymentSettings._cache.clear()
        with pytest.raises(ValueError, match="no gateway configurations"):
            TenantPaymentSettings._load_from_db(TENANT_ID)

    def test_cache_and_invalidate(self, db, factory):
        factory.tenant_payment_settings(gateway_type="stripe")
        TenantPaymentSettings._cache.clear()

        first = TenantPaymentSettings.get(TENANT_ID)
        assert TENANT_ID in TenantPaymentSettings._cache

        TenantPaymentSettings.invalidate(TENANT_ID)
        assert TENANT_ID not in TenantPaymentSettings._cache

        second = TenantPaymentSettings.get(TENANT_ID)
        assert first["tenant_id"] == second["tenant_id"]


class TestGetGateway:
    def test_returns_stripe_with_tenant_default(self, db, factory):
        factory.tenant_payment_settings(gateway_type="stripe")
        TenantPaymentSettings._cache.clear()

        gateway = get_gateway(TENANT_ID)

        assert isinstance(gateway, StripePaymentGateway)
        assert gateway.settings["secret_key"] == "sk_test_fake"

    def test_override_gateway_type(self, db, factory):
        factory.tenant_payment_settings(gateway_type="stripe")
        factory.tenant_payment_settings(
            gateway_type="paypal",
            payment_config={
                "client_id": "cid",
                "client_secret": "sec",
                "callback_base_url": "https://api.test.example",
            },
        )
        TenantPaymentSettings._cache.clear()

        from app.payments.paypal_gateway import PayPalPaymentGateway

        gateway = get_gateway(TENANT_ID, gateway_type="paypal")
        assert isinstance(gateway, PayPalPaymentGateway)

    def test_unknown_gateway_type_raises(self, db, factory):
        factory.tenant_payment_settings(gateway_type="stripe")
        TenantPaymentSettings._cache.clear()

        with pytest.raises(ValueError, match="Unknown gateway type"):
            get_gateway(TENANT_ID, gateway_type="not-a-gateway")

    def test_unconfigured_gateway_raises(self, db, factory):
        factory.tenant_payment_settings(gateway_type="stripe")
        TenantPaymentSettings._cache.clear()

        with pytest.raises(ValueError, match="no configuration for gateway"):
            get_gateway(TENANT_ID, gateway_type=GatewayType.PAYPAL)

    def test_registry_contains_all_gateway_types(self):
        assert set(GATEWAY_REGISTRY.keys()) == set(GatewayType)
