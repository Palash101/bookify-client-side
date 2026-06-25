import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.session import Base


class NotificationTemplateType(str, enum.Enum):
    email = "email"
    notification = "notification"


class NotificationTemplate(Base):
    """
    Per-tenant notification/email templates keyed by event_type (e.g. client.booking.created).
    """

    __tablename__ = "notification_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_notification_templates_tenant_name"),
        Index("ix_notification_templates_tenant_id", "tenant_id"),
        Index("ix_notification_templates_event_type", "event_type"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String(50), nullable=False)
    name = Column(String(255), nullable=False)
    event_type = Column(String(255), nullable=False)
    template_type = Column(
        "type",
        Enum(
            NotificationTemplateType,
            name="notification_template_type",
            create_type=False,
        ),
        nullable=False,
    )
    subject = Column(String(500), nullable=True)
    body = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
