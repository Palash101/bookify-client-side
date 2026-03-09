from sqlalchemy import Column, String, Integer, Date, DateTime, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.db.session import Base
import uuid


class Package(Base):
    __tablename__ = "packages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    validity_start = Column(Date, nullable=True)
    validity_end = Column(Date, nullable=True)
    validity_days = Column(Integer, nullable=True)
    sort_order = Column(Integer, nullable=True)
    package_features = Column(JSONB, nullable=True)
    terms_conditions = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True)
    status = Column(String(20), nullable=True)  # e.g. draft, active (package_status_enum)
    package_type = Column(String(20), nullable=True)  # e.g. one_time, recurring (package_type_enum)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True)
    booking_restriction = Column(JSONB, nullable=True)

    # Relationships
    pricing_list = relationship("PackagePricing", back_populates="package", lazy="select", foreign_keys="PackagePricing.package_id")
