from sqlalchemy import Column, Text, ForeignKey, Boolean, DateTime, true
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from app.core.db.session import Base
import uuid


class TenantSetting(Base):
    """
    Per-tenant key/value settings. Booking uses setting_key='gym_config' and value (JSONB).
    """

    __tablename__ = "settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    setting_key = Column(Text, nullable=False, index=True)
    value = Column(JSONB, nullable=True)
    is_enabled = Column(Boolean, nullable=True, server_default=true())
    created_at = Column(DateTime(timezone=False), server_default=func.now(), nullable=True)
    updated_at = Column(DateTime(timezone=False), server_default=func.now(), onupdate=func.now(), nullable=True)
