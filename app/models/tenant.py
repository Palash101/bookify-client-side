from sqlalchemy import Column, String, DateTime, Boolean, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.db.session import Base
import uuid


class Tenant(Base):
    __tablename__ = "tenants"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    business_name = Column(Text, nullable=True)
    domain = Column(Text, nullable=True, index=True)
    status = Column(Text, nullable=True)
    timezone = Column(Text, nullable=True)
    currency = Column(Text, nullable=True)
    terms_accepted = Column(Boolean, nullable=True)
    type = Column(Text, nullable=True)  # tenant_type enum
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True)
    
    # Relationships
    users = relationship("User", back_populates="tenant", lazy="select")
    api_keys = relationship("TenantAPIKey", back_populates="tenant", lazy="select")
