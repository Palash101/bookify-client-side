import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.session import Base


class APIKeyStatus(str, enum.Enum):
    active = "active"
    block = "block"


class OrganizationAPIKey(Base):
    __tablename__ = "organization_api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    tenant_id = Column(
        String(50),
        ForeignKey("organizations.organization_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Legacy plain-text key; NULL once hashed (tasks/sql/master/001-002 in the main backend).
    api_key = Column(Text, nullable=True)
    # sha256 of the key: what X-Tenant-Key is matched against.
    key_hash = Column(Text, nullable=True, unique=True)
    key_prefix = Column(Text, nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    status = Column(
        Enum(APIKeyStatus, name="api_key_status"),
        nullable=False,
        default=APIKeyStatus.active,
        server_default=APIKeyStatus.active.value,
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
