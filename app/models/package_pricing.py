from sqlalchemy import Column, String, Integer, Boolean, DateTime, Numeric, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.db.session import Base
import uuid


class PackagePricing(Base):
    __tablename__ = "package_pricing"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    package_id = Column(UUID(as_uuid=True), ForeignKey("packages.id"), nullable=False, index=True)
    price = Column(Numeric(10, 2), nullable=True)
    discount_id = Column(UUID(as_uuid=True), ForeignKey("package_discounts.id"), nullable=True, index=True)
    session_type = Column(String(20), nullable=True)  # e.g. sessions, class (session_type_enum)
    session_count = Column(Integer, nullable=True)
    is_unlimited = Column(Boolean, default=False, nullable=True)
    persons = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    # Relationships
    package = relationship("Package", back_populates="pricing_list", lazy="select", foreign_keys=[package_id])
    discount = relationship("PackageDiscount", lazy="select", foreign_keys=[discount_id])
