from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.tenant_website_sections import TenantWebsiteSection


class TenantWebsiteSectionsService:
    @staticmethod
    def get_active_default_sections(
        db: Session,
        tenant_id: str,
        theme_id: Optional[UUID] = None,
    ) -> Optional[Any]:
        query = db.query(TenantWebsiteSection).filter(
            TenantWebsiteSection.tenant_id == tenant_id,
            TenantWebsiteSection.is_active.is_(True),
        )
        if theme_id is not None:
            query = query.filter(TenantWebsiteSection.theme_id == theme_id)

        row = query.first()
        return row.default_sections if row else None
