from sqlalchemy import BigInteger, Boolean, Column, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.core.db.session import Base


class TenantWebsiteSection(Base):
    """
    Per-tenant website section overrides for a selected theme.
    default_sections holds the theme template; content holds tenant customizations.
    """

    __tablename__ = "tenant_website_sections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "theme_id", name="uq_tenant_website_sections_theme_tenant"),
        Index("idx_tenant_website_sections_tenant_id", "tenant_id"),
        Index("idx_tenant_website_sections_theme_id", "theme_id"),
        Index("idx_tenant_website_sections_is_active", "is_active"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(String(50), nullable=False)
    theme_id = Column(UUID(as_uuid=True), nullable=False)
    default_sections = Column(JSONB, nullable=False, server_default="[]")
    content = Column(JSONB, nullable=False, server_default="{}")
    is_active = Column(Boolean, nullable=False, server_default="true")
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
