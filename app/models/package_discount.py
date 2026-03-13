from sqlalchemy import Column, String, Text, Date, DateTime, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.core.db.session import Base
import uuid


class PackageDiscount(Base):
    __tablename__ = "package_discounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(150), nullable=True)
    description = Column(Text, nullable=True)
    value = Column(Numeric(10, 2), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    type = Column(String(20), nullable=True)  # e.g. flat, percentage (discount_type enum)
