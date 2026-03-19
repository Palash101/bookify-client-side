from sqlalchemy import Column, String, DateTime, Numeric, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from app.core.db.session import Base
import uuid


class WalletTransaction(Base):
    """
    Ledger for wallet credits/debits (top-up, purchase, refund, etc.).
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

    # Human-readable order ref e.g. ORD12233 (nullable for top-up / non-order txns)
    order_id = Column(String(50), nullable=True, index=True)

    # credit | debit
    direction = Column(String(10), nullable=False)

    # topup, purchase, refund, adjustment, reversal, etc.
    transaction_type = Column(String(50), nullable=False)

    # External/gateway transaction id
    transaction_id = Column(String(255), nullable=True, index=True)

    # pending | succeeded | failed | reversed | cancelled
    status = Column(String(20), nullable=False, default="pending")

    metadata_ = Column("metadata", JSONB, nullable=True)

    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), nullable=False)

    balance_before = Column(Numeric(12, 2), nullable=True)
    balance_after = Column(Numeric(12, 2), nullable=True)

    # Role that created this txn (e.g. admin, member, system)
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
