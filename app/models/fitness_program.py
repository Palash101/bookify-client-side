from sqlalchemy import (
    Column,
    String,
    Text,
    DateTime,
    Boolean,
    Integer,
    BigInteger,
    ForeignKey,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.session import Base


class FitnessProgram(Base):
    __tablename__ = "fitness_programs"

    id = Column(BigInteger, primary_key=True, index=True)

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    location_id = Column(UUID(as_uuid=True), ForeignKey("locations.id"), nullable=True, index=True)

    name = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    image_url = Column(Text, nullable=True)

    is_active = Column(Boolean, nullable=True, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=True,
    )

    is_layout_required = Column(Boolean, nullable=True, default=False)
    spot_name = Column(String(50), nullable=True)
    show_spots_left = Column(Boolean, nullable=True, default=False)
    spots_left_label = Column(String(50), nullable=True)
    classes_title_key = Column(String(100), nullable=True)

    experience_required = Column(Boolean, nullable=True, default=False)
    disallow_first_timers = Column(Boolean, nullable=True, default=False)
    minimum_experience_level = Column(String(50), nullable=True)

    has_age_restriction = Column(Boolean, nullable=True, default=False)
    min_age = Column(Integer, nullable=True)
    max_age = Column(Integer, nullable=True)

    training_mode = Column(String(20), nullable=True)  # e.g. group / personal
    gender_restriction = Column(String(10), nullable=True)  # e.g. male / female / mixed

    display_position = Column(Integer, nullable=True)

