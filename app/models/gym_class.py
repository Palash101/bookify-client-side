from sqlalchemy import Column, String, BigInteger, Date, Time, DateTime, ForeignKey, Text, Numeric, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.db.session import Base
import uuid


class GymClass(Base):
    __tablename__ = "gym_classes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    training_programme_id = Column(BigInteger, nullable=True, default=0)
    title = Column(String(255), nullable=True)
    theme_name = Column(String(255), nullable=True)
    trainer_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    class_date = Column(Date, nullable=True, index=True)
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    max_bookings = Column(Integer, nullable=True)
    max_waitings = Column(Integer, nullable=True)
    booking_counts = Column(Integer, nullable=True)
    attendance_count = Column(Integer, nullable=True)
    booking_type = Column(String(50), nullable=True)
    price = Column(Numeric(10, 2), nullable=True)
    gender = Column(String(20), nullable=True)
    terms_text = Column(Text, nullable=True)
    status = Column(String(50), nullable=True)
    publish_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True)
    schedule_id = Column(BigInteger, ForeignKey("class_schedules.id"), nullable=True, index=True)
    # Legacy layout pointer
    layout_id = Column(BigInteger, nullable=True)
    # Optional inline layout payload (newer schema)
    layouts = Column(JSONB, nullable=True)

    # Relationships
    schedule = relationship("ClassSchedule", back_populates="gym_classes", lazy="select", foreign_keys=[schedule_id])
