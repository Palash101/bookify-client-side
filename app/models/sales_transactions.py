from sqlalchemy import (
    BigInteger,
    Column,
    String,
    DateTime,
    Numeric,
    ForeignKey,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from app.core.db.session import Base


class SalesTransactions(Base):
    """
    Timeline rows for a sale. Matches public.sales_transactions (minimal columns):
    payment_method, gateway, gateway_txn_id, status, amount, currency, source,
    user_id, created_by_type, created_by_id. Package/session snapshot lives on sales.
    """

    __tablename__ = "sales_transactions"

    # DB uses bigint / bigserial (not UUID).
    id = Column(BigInteger, primary_key=True, autoincrement=True)

    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sales.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    tenant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # wallet_add | package_gateway | package_wallet
    payment_method = Column(String(20), nullable=False, server_default="package_gateway", index=True)

    gateway = Column(String, nullable=False)
    gateway_txn_id = Column(Text, nullable=True, index=True)

    status = Column(String, nullable=False)

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

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
