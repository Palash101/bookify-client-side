from sqlalchemy import (
    Column,
    String,
    Integer,
    Text,
    ForeignKey,
    DateTime,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.session import Base
import uuid
from typing import Optional


class ClassBooking(Base):
    __tablename__ = "class_bookings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    class_id = Column(UUID(as_uuid=True), ForeignKey("gym_classes.id", ondelete="CASCADE"), nullable=False, index=True)

    seat_id = Column(String(64), nullable=True)

    status = Column(String(20), nullable=False, index=True)
    waiting_position = Column(Integer, nullable=True)

    booked_at = Column(DateTime(timezone=True), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    # cash | wallet | package | gateway | free
    payment_mode = Column(String(20), nullable=True)
    # DB column name is user_package_id (legacy). Keep Python API name stable.
    user_package_purchase_id = Column(
        "user_package_id",
        UUID(as_uuid=True),
        ForeignKey("sales.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Human-readable booking order reference e.g. ORD1A2B3C4D
    order_id = Column(String(50), nullable=True, index=True)

    sessions_deducted = Column(Integer, nullable=False, server_default="0")
    promoted_from_waiting_at = Column(DateTime(timezone=True), nullable=True)

    cancelled_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    cancellation_reason = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    checkin_time = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    @property
    def payment_method(self) -> Optional[str]:
        # Backwards-compatible alias (older code / payloads).
        return self.payment_mode

    @payment_method.setter
    def payment_method(self, value: Optional[str]) -> None:
        self.payment_mode = value
