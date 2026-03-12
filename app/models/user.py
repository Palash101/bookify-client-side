from sqlalchemy import Column, String, Boolean, DateTime, Date, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from typing import TYPE_CHECKING, Optional, Dict, Any
from app.core.db.session import Base
import uuid

# Import Role and Tenant to ensure they're registered before User relationship is set up
from app.models.role import Role  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401


class User(Base):
    __tablename__ = "users"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    role_id = Column(UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False, index=True)
    
    email = Column(Text, nullable=True, index=True)
    phone = Column(Text, nullable=True)
    password_hash = Column(Text, nullable=True)
    
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    avatar = Column(Text, nullable=True)
    gender = Column(String(20), nullable=True)
    dob = Column(Date, nullable=True)
    designation = Column(String(100), nullable=True)
    skills = Column(JSONB, nullable=True)
    
    is_active = Column(Boolean, default=True, nullable=True)
    user_type = Column(String(20), nullable=False, server_default="user")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True)
    
    # Relationships
    tenant = relationship("Tenant", back_populates="users", lazy="select")
    role = relationship("Role", back_populates="users", lazy="select")
