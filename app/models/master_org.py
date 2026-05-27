import uuid

from sqlalchemy import BigInteger, Column, DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.db.session import Base


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    organization_id = Column(String(100), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    domain = Column(String(255), nullable=False, unique=True, index=True)
    db_id = Column(UUID(as_uuid=True), nullable=True)
    api_version = Column(String(50), nullable=False)
    status = Column(
        Enum("active", "block", name="organization_status"),
        nullable=False,
        server_default="active",
    )
    onboarding = Column(
        Enum("draft", "initilized", "completed", name="organization_onboarding"),
        nullable=False,
        server_default="draft",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Organization(id={self.id}, organization_id={self.organization_id}, name={self.name})>"
