import enum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    event,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from app.core.db.session import Base
from app.core.snowflake import generate_txn_ref


class SalesTransactionStatus(str, enum.Enum):
    pending = "pending"
    failed = "failed"
    success = "success"
    cancelled = "cancelled"


TERMINAL_SALES_TRANSACTION_STATUSES = frozenset(
    {
        SalesTransactionStatus.cancelled,
        SalesTransactionStatus.failed,
        SalesTransactionStatus.success,
    }
)


def normalize_sales_transaction_status(value: Any) -> SalesTransactionStatus:
    if isinstance(value, SalesTransactionStatus):
        return value
    raw = value.value if hasattr(value, "value") else str(value)
    mapping = {
        "pending": SalesTransactionStatus.pending,
        "failed": SalesTransactionStatus.failed,
        "success": SalesTransactionStatus.success,
        "succeeded": SalesTransactionStatus.success,
        "cancelled": SalesTransactionStatus.cancelled,
        "canceled": SalesTransactionStatus.cancelled,
        "reversed": SalesTransactionStatus.failed,
    }
    return mapping.get(raw.strip().lower(), SalesTransactionStatus.pending)


def sales_transaction_status_from_gateway(status_value: Any) -> SalesTransactionStatus:
    raw = status_value.value if hasattr(status_value, "value") else str(status_value)
    normalized = raw.strip().lower()
    if normalized in ("success", "succeeded"):
        return SalesTransactionStatus.success
    if normalized in ("failed",):
        return SalesTransactionStatus.failed
    if normalized in ("cancelled", "canceled"):
        return SalesTransactionStatus.cancelled
    if normalized in ("refunded", "reversed"):
        return SalesTransactionStatus.failed
    if normalized in ("pending",):
        return SalesTransactionStatus.pending
    return normalize_sales_transaction_status(normalized)


class SalesTransactions(Base):
    """
    Timeline rows for a sale. Matches public.sales_transactions (minimal columns):
    payment_method, gateway, gateway_txn_id, status, amount, currency, source,
    location_id, user_id, created_by_type, created_by_id. Package/session snapshot lives on sales.
    """

    __tablename__ = "sales_transactions"

    # DB uses bigint / bigserial (not UUID).
    id = Column(BigInteger, primary_key=True, autoincrement=True)

    sales_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sales.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    location_id = Column(
        UUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # wallet_add | package_gateway | package_wallet
    payment_method = Column(String(20), nullable=False, server_default="package_gateway", index=True)

    gateway = Column(String, nullable=False)
    gateway_txn_id = Column(Text, nullable=True, index=True)

    status = Column(
        Enum(SalesTransactionStatus, name="sales_transaction_status", create_type=False),
        nullable=False,
        server_default=SalesTransactionStatus.pending.value,
    )

    amount = Column(Numeric(10, 2), nullable=True)
    currency = Column(String(3), nullable=True)

    source = Column(String(50), nullable=True)

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_type = Column(String(50), nullable=True)
    created_by_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Extra context for flows where Sale is created only on success.
    extra_metadata = Column(JSONB, nullable=True)

    # Auto-generated ref, e.g. TXN-185942817304592384
    txn_ref = Column(
        String(32),
        unique=True,
        nullable=False,
        index=True,
        default=generate_txn_ref,
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


@event.listens_for(SalesTransactions, "before_insert")
def _assign_txn_ref(mapper, connection, target: SalesTransactions) -> None:
    """Allocate ``TXN-{snowflake}`` when not already set."""
    if not getattr(target, "txn_ref", None):
        target.txn_ref = generate_txn_ref()
