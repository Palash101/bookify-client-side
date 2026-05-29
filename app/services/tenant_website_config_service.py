from typing import Optional

from sqlalchemy.orm import Session

from app.models.tenant_website_config import TenantWebsiteConfig


class TenantWebsiteConfigService:
    @staticmethod
    def get_active_config(
        db: Session, tenant_id: str
    ) -> Optional[TenantWebsiteConfig]:
        return (
            db.query(TenantWebsiteConfig)
            .filter(
                TenantWebsiteConfig.tenant_id == tenant_id,
                TenantWebsiteConfig.is_active.isnot(False),
            )
            .first()
        )
