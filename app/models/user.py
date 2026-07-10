from sqlalchemy import Column, String, Boolean, DateTime, Date, ForeignKey, Text, Index, Numeric, Enum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from typing import TYPE_CHECKING, Optional, Dict, Any
from app.core.db.session import Base
import enum
import uuid

# Import Role and Tenant to ensure they're registered before User relationship is set up
from app.models.role import Role  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401


class UserGender(str, enum.Enum):
    male = "male"
    female = "female"


def user_gender_value(gender: Any) -> Optional[str]:
    if gender is None:
        return None
    if isinstance(gender, UserGender):
        return gender.value
    return str(gender).strip().lower() or None


def normalize_user_gender(value: Any) -> Optional[UserGender]:
    if value is None:
        return None
    if isinstance(value, UserGender):
        return value
    raw = value.value if hasattr(value, "value") else str(value)
    s = raw.strip().lower()
    if not s:
        return None
    if s in ("male", "m", "man", "men"):
        return UserGender.male
    if s in ("female", "f", "woman", "women"):
        return UserGender.female
    return None


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Optimized index for tenant + email lookups (login, auth, etc.)
        Index("ix_users_tenant_email", "tenant_id", "email"),
    )
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    role_id = Column(UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False, index=True)
    
    email = Column(Text, nullable=True, index=True)
    phone = Column(Text, nullable=True)
    password_hash = Column(Text, nullable=True)
    
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    avatar = Column(Text, nullable=True)
    gender = Column(
        Enum(UserGender, name="user_gender_enum", create_type=False),
        nullable=True,
    )
    dob = Column(Date, nullable=True)
    designation = Column(String(100), nullable=True)
    skills = Column(JSONB, nullable=True)
    
    is_active = Column(Boolean, default=True, nullable=True)
    # user_type: "member", "client", "admin" etc.
    user_type = Column(String(20), nullable=False, server_default="member")

    # Wallet balance (amount available for purchases)
    wallet = Column(Numeric(12, 2), nullable=True, server_default="0")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True)
    
    # Relationships
    tenant = relationship("Tenant", back_populates="users", lazy="select")
    role = relationship("Role", back_populates="users", lazy="select")
