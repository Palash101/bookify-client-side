from typing import Any, Optional

from sqlalchemy import Column, String, DateTime, Numeric, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import object_session, relationship
from sqlalchemy.sql import func

from app.core.db.session import Base
import uuid


class WalletTransaction(Base):
    """
    Wallet ledger row (amounts + Stripe session id).
    Context (type) lives on ``reference_type`` / ``reference_id``.
    Class-booking wallet rows use ``ClassBooking.notes`` markers (no sale).
    """

    __tablename__ = "wallet_transactions"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True,
    )

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    direction = Column(String(10), nullable=False)

    transaction_id = Column(String(255), nullable=True, index=True)

    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), nullable=False)

    balance_before = Column(Numeric(12, 2), nullable=True)
    balance_after = Column(Numeric(12, 2), nullable=True)

    created_by = Column(String(50), nullable=True)

    created_by_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    reference_type = Column(String(32), nullable=True)
    reference_id = Column(UUID(as_uuid=True), nullable=True)

    linked_sale = relationship(
        "Sale",
        primaryjoin="WalletTransaction.id==Sale.wallet_transaction_id",
        foreign_keys="Sale.wallet_transaction_id",
        uselist=False,
        viewonly=True,
    )

    @property
    def transaction_type(self) -> str:
        if self.reference_type:
            return str(self.reference_type)
        sess = object_session(self)
        if sess is None:
            return ""
        from app.models.class_booking import ClassBooking

        tid = str(self.id)
        if sess.query(ClassBooking).filter(ClassBooking.notes.contains(f"__bfy_wtxn:{tid}:refund")).first():
            return "class_booking_refund"
        if sess.query(ClassBooking).filter(ClassBooking.notes.contains(f"__bfy_wtxn:{tid}:debit")).first():
            return "class_booking"
        return ""

    @transaction_type.setter
    def transaction_type(self, value: Optional[str]) -> None:
        self.reference_type = value

    @property
    def order_id(self) -> Optional[str]:
        if self.reference_id is not None:
            return str(self.reference_id)
        if self.linked_sale:
            return str(self.linked_sale.id)
        return None

    @order_id.setter
    def order_id(self, value: Optional[str]) -> None:
        if value is None or str(value) == "":
            self.reference_id = None
            return
        try:
            self.reference_id = uuid.UUID(str(value))
        except (ValueError, TypeError):
            return

    @property
    def status(self) -> str:
        tt = self.transaction_type
        if tt in ("class_booking", "class_booking_refund"):
            return "succeeded"
        sale = self.linked_sale
        sess = object_session(self)
        if sale is not None and sess is not None:
            from app.models.sales import sale_status_value

            return sale_status_value(sess, sale)
        return "pending"

    @status.setter
    def status(self, value: Optional[str]) -> None:
        return

    @property
    def metadata_(self) -> Optional[dict[str, Any]]:
        tt = self.transaction_type
        if not tt:
            return None
        return {"transaction_type": tt, "status": self.status}
