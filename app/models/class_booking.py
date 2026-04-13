from sqlalchemy import (
    Column,
    String,
    Integer,
    Boolean,
    Text,
    ForeignKey,
    Numeric,
    DateTime,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.session import Base
import uuid


class ClassBooking(Base):
    __tablename__ = "class_bookings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    class_id = Column(UUID(as_uuid=True), ForeignKey("gym_classes.id", ondelete="CASCADE"), nullable=False, index=True)

    booking_type = Column(String(20), nullable=False)
    seat_id = Column(String(64), nullable=True)
    trainer_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    status = Column(String(20), nullable=False, index=True)
    waiting_position = Column(Integer, nullable=True)

    booked_at = Column(DateTime(timezone=True), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    payment_method = Column(String(20), nullable=True)
    package_id = Column(UUID(as_uuid=True), ForeignKey("packages.id", ondelete="SET NULL"), nullable=True)
    # DB column name is user_package_id (legacy). Keep Python API name stable.
    user_package_purchase_id = Column(
        "user_package_id",
        UUID(as_uuid=True),
        ForeignKey("sales.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Human-readable booking order reference e.g. ORD1A2B3C4D
    order_id = Column(String(50), nullable=True, index=True)

    gateway_order_id = Column(String(255), nullable=True)
    gateway_payment_id = Column(String(255), nullable=True)

    credits_deducted = Column(Numeric(12, 4), nullable=True)
    wallet_txn_id = Column(UUID(as_uuid=True), ForeignKey("wallet_transactions.id", ondelete="SET NULL"), nullable=True)

    sessions_deducted = Column(Integer, nullable=False, server_default="0")

    payment_held = Column(Boolean, nullable=False, server_default="false")
    promoted_from_waiting_at = Column(DateTime(timezone=True), nullable=True)

    cancelled_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    cancellation_reason = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    checkin_time = Column(DateTime(timezone=True), nullable=True)
    checked_in = Column(Boolean, nullable=False, server_default="false")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
