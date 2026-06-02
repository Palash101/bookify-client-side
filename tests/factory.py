"""
Test data factory — create tenants, users, classes, bookings, sales, payment settings.
"""

from __future__ import annotations

import uuid
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.class_booking import ClassBooking
from app.models.gym_class import GymClass
from app.models.package import Package
from app.models.role import Role
from app.models.sales import Sale
from app.models.tenant import Tenant
from app.models.tenant_payment_settings import TenantPaymentSettings
from app.models.user import User

def layout_with_seats(
    seat_ids: list[str],
    *,
    statuses: Optional[list[str]] = None,
) -> dict[str, Any]:
    statuses = statuses or ["available"] * len(seat_ids)
    seats = [
        {"id": sid, "status": statuses[i]}
        for i, sid in enumerate(seat_ids)
    ]
    return {"totalSeats": len(seats), "seats": seats}


_DEFAULT_STRIPE_CONFIG = {
    "secret_key": "sk_test_fake",
    "webhook_secret": "whsec_fake",
    "callback_base_url": "https://api.test.example",
}


class TestDataFactory:
    def __init__(self, db: Session, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self._user_counter = 0

    def _ensure_tenant(self) -> Tenant:
        tenant = self.db.query(Tenant).filter(Tenant.id == self.tenant_id).first()
        if tenant is None:
            tenant = Tenant(
                id=self.tenant_id,
                business_name="Test Gym",
                status="active",
                timezone="UTC",
            )
            self.db.add(tenant)
            self.db.flush()
        return tenant

    def _ensure_member_role(self) -> Role:
        role = self.db.query(Role).filter(Role.name == "member").first()
        if role is None:
            role = Role(id=uuid.uuid4(), name="member", key="member")
            self.db.add(role)
            self.db.flush()
        return role

    def user(
        self,
        *,
        email: Optional[str] = None,
        gender: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> User:
        self._ensure_tenant()
        role = self._ensure_member_role()
        self._user_counter += 1
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_id or self.tenant_id,
            role_id=role.id,
            email=email or f"user{self._user_counter}@test.example",
            first_name="Test",
            last_name=f"User{self._user_counter}",
            gender=gender,
            is_active=True,
            user_type="member",
            wallet=Decimal("0"),
        )
        self.db.add(user)
        self.db.flush()
        return user

    def package(self, *, name: str = "Test Package") -> Package:
        self._ensure_tenant()
        pkg = Package(
            id=uuid.uuid4(),
            name=name,
            tenant_id=self.tenant_id,
            status="active",
        )
        self.db.add(pkg)
        self.db.flush()
        return pkg

    def package_sale(
        self,
        *,
        user: User,
        sessions_remaining: int = 10,
        sale_status: str = "succeeded",
    ) -> Sale:
        pkg = self.package()
        sale = self.sale(
            user=user,
            sessions_remaining=sessions_remaining,
            extra_metadata={"status": sale_status},
        )
        sale.package_id = pkg.id
        sale.type = "package_wallet"
        sale.status = sale_status
        self.db.flush()
        return sale

    def gym_class(
        self,
        *,
        class_date: Optional[date] = None,
        start_time: Optional[time] = None,
        max_bookings: int = 10,
        max_waitings: int = 5,
        booking_counts: int = 0,
        status: str = "active",
        price: Optional[Decimal] = None,
        gender: Optional[str] = None,
        booking_type: Optional[str] = None,
        layouts: Optional[dict] = None,
        trainer_id: Optional[uuid.UUID] = None,
    ) -> GymClass:
        if class_date is None:
            class_date = date.today() + timedelta(days=7)
        if start_time is None:
            start_time = time(10, 0)

        gc = GymClass(
            id=uuid.uuid4(),
            title="Test Class",
            class_date=class_date,
            start_time=start_time,
            end_time=time(11, 0),
            max_bookings=max_bookings,
            max_waitings=max_waitings,
            booking_counts=booking_counts,
            trainer_id=trainer_id,
            status=status,
            price=price or Decimal("0"),
            gender=gender,
            booking_type=booking_type,
            layouts=layouts,
        )
        self.db.add(gc)
        self.db.flush()
        return gc

    def booking(
        self,
        *,
        user: User,
        gym_class: GymClass,
        status: str = "confirmed",
        payment_mode: str = "free",
        sessions_deducted: int = 0,
        user_package_purchase_id: Optional[uuid.UUID] = None,
        seat_id: Optional[str] = None,
        waiting_position: Optional[int] = None,
    ) -> ClassBooking:
        b = ClassBooking(
            id=uuid.uuid4(),
            tenant_id=user.tenant_id,
            user_id=user.id,
            class_id=gym_class.id,
            status=status,
            payment_mode=payment_mode,
            sessions_deducted=sessions_deducted,
            user_package_purchase_id=user_package_purchase_id,
            seat_id=seat_id,
            waiting_position=waiting_position,
        )
        self.db.add(b)
        self.db.flush()
        return b

    def sale(
        self,
        *,
        user: User,
        sessions_remaining: Optional[int] = None,
        amount: Decimal = Decimal("100.00"),
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> Sale:
        meta = dict(extra_metadata or {})
        if sessions_remaining is not None:
            meta["sessions_remaining"] = sessions_remaining
        sale = Sale(
            id=uuid.uuid4(),
            tenant_id=user.tenant_id,
            user_id=user.id,
            amount=amount,
            type="package_wallet",
            extra_metadata=meta or None,
        )
        self.db.add(sale)
        self.db.flush()
        return sale

    def tenant_payment_settings(
        self,
        *,
        gateway_type: str = "stripe",
        payment_config: Optional[dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> TenantPaymentSettings:
        self._ensure_tenant()
        row = TenantPaymentSettings(
            id=uuid.uuid4(),
            tenant_id=tenant_id or self.tenant_id,
            gateway_type=gateway_type,
            payment_config=payment_config or _DEFAULT_STRIPE_CONFIG,
        )
        self.db.add(row)
        self.db.flush()
        return row
