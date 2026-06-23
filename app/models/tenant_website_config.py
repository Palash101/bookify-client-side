from sqlalchemy import Boolean, Column, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.core.db.session import Base
import uuid


class TenantWebsiteConfig(Base):
    """
    Per-tenant public website / branding configuration.
    Lives in the tenant (shared) database, not the master control-plane DB.
    """

    __tablename__ = "tenant_website_config"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    tenant_id = Column(String, unique=True, nullable=False, index=True)

    # Theme
    theme_id = Column(UUID(as_uuid=True), nullable=True)
    theme_name = Column(String(100), nullable=True)

    about = Column(Text, nullable=True)
    logo_url = Column(Text, nullable=True)
    footer_logo = Column(Text, nullable=True)
    fevicon_icon = Column(Text, nullable=True)
    primary_color = Column(String(50), nullable=True)
    secondary_color = Column(String(50), nullable=True)
    background_color = Column(String(50), nullable=True)

    # Business hours (e.g. {"mon": {"open": "09:00", "close": "18:00"}, ...})
    business_hours = Column(JSONB, nullable=True)

    support_email = Column(String(255), nullable=True)
    support_phone = Column(String(50), nullable=True)

    terms_and_conditions = Column(Text, nullable=True)
    privacy_policy = Column(Text, nullable=True)
    refund_policy = Column(Text, nullable=True)
    terms_last_updated_at = Column(DateTime(timezone=True), nullable=True)

    tax_id = Column(String(100), nullable=True)
    tax_id_type = Column(String(50), nullable=True)
    invoice_prefix = Column(String(50), nullable=True)
    invoice_footer_note = Column(Text, nullable=True)

    social_links = Column(JSONB, nullable=True)

    is_active = Column(Boolean, nullable=True, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=True,
    )
