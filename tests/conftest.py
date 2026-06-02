"""
Shared pytest fixtures — mirrors trainhub-backend style (db, factory, current_user).

Uses in-memory SQLite with PostgreSQL type shims (UUID, JSONB) so tests run
without tenant GCP credentials.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Generator
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db.session import Base
from app.models.class_booking import ClassBooking  # noqa: F401
from app.models.class_schedule import ClassSchedule  # noqa: F401
from app.models.gym_class import GymClass  # noqa: F401
from app.models.location import Location  # noqa: F401
from app.models.otp import OTP  # noqa: F401
from app.models.package import Package  # noqa: F401
from app.models.package_discount import PackageDiscount  # noqa: F401
from app.models.package_pricing import PackagePricing  # noqa: F401
from app.models.role import Role
from app.models.sales import Sale  # noqa: F401
from app.models.tenant import Tenant
from app.models.tenant_api_key import TenantAPIKey  # noqa: F401
from app.models.tenant_payment_settings import TenantPaymentSettings  # noqa: F401
from app.models.tenant_setting import TenantSetting  # noqa: F401
from app.models.tenant_website_config import TenantWebsiteConfig  # noqa: F401
from app.models.user import User
from app.models.user_package import UserPackage  # noqa: F401
from app.models.wallet_transactions import WalletTransaction  # noqa: F401
from app.payments.factory import TenantPaymentSettings as TenantPaymentSettingsLoader
from app.schemas.gym_config_value import (
    BookingSettingsConfig,
    GymConfigValue,
    PaymentPricingConfig,
)
from tests.factory import TestDataFactory

TENANT_ID = "test-tenant-001"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(element, compiler, **kw):
    return "VARCHAR(36)"


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _sqlite_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)


@pytest.fixture
def db(engine) -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, expire_on_commit=False)
    session = SessionLocal()
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(autouse=True)
def _clear_payment_settings_cache():
    TenantPaymentSettingsLoader._cache.clear()
    yield
    TenantPaymentSettingsLoader._cache.clear()


@pytest.fixture(autouse=True)
def _patch_tenant_session_factory(db):
    """Payment factory opens short-lived sessions on the test connection (same transaction)."""
    payment_session_factory = sessionmaker(
        bind=db.connection(), expire_on_commit=False
    )
    with patch(
        "app.payments.factory.get_session_factory",
        return_value=payment_session_factory,
    ):
        yield


@pytest.fixture
def factory(db: Session) -> TestDataFactory:
    return TestDataFactory(db, tenant_id=TENANT_ID)


@pytest.fixture
def gym_config() -> GymConfigValue:
    return GymConfigValue(
        booking_settings=BookingSettingsConfig(
            allow_late_cancellations=False,
            cancellation_window_hours=0,
            allow_waiting_list=True,
            auto_confirm_booking=True,
        )
    )


@pytest.fixture
def booking_gym_config() -> GymConfigValue:
    """Permissive gym config for booking validate/create flows."""
    return GymConfigValue(
        payment_pricing=PaymentPricingConfig(
            enable_free_classes=True,
            enable_class_package=True,
            enable_pay_per_class=True,
            currency="QAR",
        ),
        booking_settings=BookingSettingsConfig(
            allow_waiting_list=True,
            auto_confirm_booking=True,
            allow_late_cancellations=False,
            cancellation_window_hours=0,
            advance_booking_window_days=365,
            booking_cutoff_minutes=0,
        ),
    )


@pytest.fixture
def current_user(factory: TestDataFactory) -> User:
    return factory.user()


@pytest.fixture
def tenant_and_role(db: Session) -> tuple[Tenant, Role]:
    tenant = db.query(Tenant).filter(Tenant.id == TENANT_ID).first()
    if tenant is None:
        tenant = Tenant(id=TENANT_ID, business_name="Test Gym", status="active", timezone="UTC")
        db.add(tenant)
    role = db.query(Role).filter(Role.name == "member").first()
    if role is None:
        role = Role(id=uuid.uuid4(), name="member", key="member")
        db.add(role)
    db.flush()
    return tenant, role


@pytest.fixture(autouse=True)
def _seed_tenant_and_role(db: Session, tenant_and_role):
    db.flush()
