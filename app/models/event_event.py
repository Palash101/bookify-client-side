import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.session import Base


class EventType(str, enum.Enum):
    standard = "standard"
    challenge = "challenge"


class EventStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"
    draft = "draft"


class EventEvent(Base):
    """
    Tenant-scoped gym events / challenges (shared tenant DB: event_events).
    """

    __tablename__ = "event_events"
    __table_args__ = (
        Index("ix_events_name", "name"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    type = Column(
        Enum(EventType, name="event_type", create_type=False),
        nullable=False,
    )
    status = Column(
        Enum(EventStatus, name="event_status", create_type=False),
        nullable=False,
        server_default=EventStatus.draft.value,
    )
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    max_participants = Column(Integer, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    image_path = Column(Text, nullable=True)
    location = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    btn_name = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)
