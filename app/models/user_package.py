from sqlalchemy import Column, String, Integer, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.session import Base
import uuid


class UserPackage(Base):
    """
    User entitlement row linked to a package purchase (sale) and pricing option.
    Mirrors public.user_packages in PostgreSQL.
    """

    __tablename__ = "user_packages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    package_id = Column(
        UUID(as_uuid=True),
        ForeignKey("packages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    pricing_id = Column(
        UUID(as_uuid=True),
        ForeignKey("package_pricing.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sale_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sales.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    expire_at = Column(DateTime(timezone=True), nullable=True)
    session_count = Column(Integer, nullable=True)
    # DB uses session_type_enum; store as string (e.g. sessions, class)
    session_type = Column(String(20), nullable=True)
    person_count = Column(Integer, nullable=True)

    created_by = Column(String(50), nullable=True)
    created_by_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
