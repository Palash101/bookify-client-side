from sqlalchemy import Column, String, DateTime, JSON, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.session import Base


class TenantPaymentSettings(Base):
    __tablename__ = "tenant_payment_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)

    # 'stripe' | 'paypal' | 'myfatoorah' (backed by payment_gateway_type enum in DB)
    gateway_type = Column(String, nullable=False)

    # Provider-specific configuration JSON
    payment_config = Column(JSON, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=True,
    )

