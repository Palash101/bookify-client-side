from typing import Any, Optional

from sqlalchemy import Column, String, DateTime, Numeric, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import object_session, relationship
from sqlalchemy.sql import func

from app.core.db.session import Base
from app.models.sales import SALE_WALLET_TXN_KEY, merge_sale_wallet_txn_meta
import uuid


class WalletTransaction(Base):
    """
    Wallet ledger row (amounts + Stripe session id). Context (type, status, order link)
    lives on the related ``Sale.extra_metadata["wallet_txn"]`` when ``Sale.wallet_transaction_id``
    points here. Class-booking wallet rows use ``ClassBooking.notes`` markers (no sale).
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

    linked_sale = relationship(
        "Sale",
        primaryjoin="WalletTransaction.id==Sale.wallet_transaction_id",
        foreign_keys="Sale.wallet_transaction_id",
        uselist=False,
        viewonly=True,
    )

    def _wallet_txn_blob(self) -> dict[str, Any]:
        sale = self.linked_sale
        if not sale or not sale.extra_metadata:
            return {}
        w = sale.extra_metadata.get(SALE_WALLET_TXN_KEY)
        return dict(w) if isinstance(w, dict) else {}

    @property
    def transaction_type(self) -> str:
        w = self._wallet_txn_blob()
        if w.get("transaction_type"):
            return str(w["transaction_type"])
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
        sale = self.linked_sale
        if sale:
            merge_sale_wallet_txn_meta(sale, transaction_type=value)

    @property
    def order_id(self) -> Optional[str]:
        if self.linked_sale:
            return str(self.linked_sale.id)
        return None

    @order_id.setter
    def order_id(self, value: Optional[str]) -> None:
        return

    @property
    def status(self) -> str:
        w = self._wallet_txn_blob()
        if w.get("status"):
            return str(w["status"])
        tt = self.transaction_type
        if tt in ("class_booking", "class_booking_refund"):
            return "succeeded"
        return "pending"

    @status.setter
    def status(self, value: Optional[str]) -> None:
        sale = self.linked_sale
        if sale:
            merge_sale_wallet_txn_meta(sale, status=value)

    @property
    def metadata_(self) -> Optional[dict[str, Any]]:
        """Response helper: wallet_txn blob from linked sale (class booking: None)."""
        w = self._wallet_txn_blob()
        return w if w else None
