from sqlalchemy import Column, String, DateTime, Numeric, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from app.core.db.session import Base
import uuid


class PackageOrder(Base):
    # DB table has been renamed to "package_purchase"
    __tablename__ = "package_purchase"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    package_id = Column(UUID(as_uuid=True), ForeignKey("packages.id"), nullable=False, index=True)

    amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(3), nullable=False)
    gateway = Column(String, nullable=False)
    gateway_transaction_id = Column(Text, nullable=True, index=True)

    status = Column(String, nullable=False, default="pending")

    # Extra context about the order (client device, source, etc.)
    extra_metadata = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=True,
    )

    # When this purchased package expires for the user
    expires_at = Column(DateTime(timezone=True), nullable=True)

