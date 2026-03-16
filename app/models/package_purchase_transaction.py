from sqlalchemy import (
    Column,
    String,
    DateTime,
    Numeric,
    ForeignKey,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
import uuid

from app.core.db.session import Base


class PackagePurchaseTransaction(Base):
    __tablename__ = "package_purchase_transactions"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True,
    )

    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("package_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    tenant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    gateway = Column(String, nullable=False)
    # Can be NULL for initial "created" event before gateway returns an ID
    gateway_txn_id = Column(Text, nullable=True, index=True)
    event_type = Column(String, nullable=False)
    status = Column(String, nullable=False)

    amount = Column(Numeric(10, 2), nullable=True)
    currency = Column(String(3), nullable=True)

    raw_payload = Column(JSONB, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

