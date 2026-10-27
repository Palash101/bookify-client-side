import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.session import Base


class EventEnroll(Base):
    """
    User enrollment in a tenant event (shared tenant DB: event_enroll).
    """

    __tablename__ = "event_enroll"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_event_enroll_event_user"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    tenant_id = Column(String, nullable=False, index=True)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_id = Column(
        UUID(as_uuid=True),
        ForeignKey("event_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Challenge progress — written only by fitnez_activites; read-only here.
    progress_count = Column(Integer, nullable=False, server_default="0")
    eligible_at = Column(DateTime(timezone=True), nullable=True)
    last_progress_at = Column(DateTime(timezone=True), nullable=True)
    final_rank = Column(Integer, nullable=True)
