from typing import Optional

from sqlalchemy.orm import Session

from app.core.redis.cache import cache
from app.models.tenant_website_config import TenantWebsiteConfig
from app.schemas.tenant_website_config import TenantWebsiteConfigData

# Branding changes rarely, but must not go stale for long.
CACHE_TTL = 300


class TenantWebsiteConfigService:
    @staticmethod
    def cache_key(tenant_id: str) -> str:
        """One key per tenant, holding its active website config."""
        return f"web_config:{tenant_id}:active"

    @staticmethod
    def invalidate(tenant_id: str) -> int:
        """Drop the cached config after a branding write."""
        return cache.delete(TenantWebsiteConfigService.cache_key(tenant_id))

    @staticmethod
    def get_active_config(
        db: Session, tenant_id: str
    ) -> Optional[TenantWebsiteConfigData]:
        """
        The tenant's active website config, served from Redis.

        Concurrent misses each run the query: it is a single indexed lookup,
        so a fill lock would cost more than it saves. A tenant with no config
        is not cached, so it is re-checked on the next request.
        """

        def loader() -> Optional[TenantWebsiteConfigData]:
            row = (
                db.query(TenantWebsiteConfig)
                .filter(
                    TenantWebsiteConfig.tenant_id == tenant_id,
                    TenantWebsiteConfig.is_active.isnot(False),
                )
                .first()
            )
            return TenantWebsiteConfigData.model_validate(row) if row else None

        return cache.get_or_set(
            TenantWebsiteConfigService.cache_key(tenant_id),
            loader,
            ttl=CACHE_TTL,
            model=TenantWebsiteConfigData,
        )
