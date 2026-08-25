from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.redis.cache import cache
from app.models.tenant_website_sections import TenantWebsiteSection

# Section templates change rarely, but must not go stale for long.
CACHE_TTL = 300


class TenantWebsiteSectionsService:
    @staticmethod
    def cache_key(tenant_id: str, theme_id: Optional[UUID] = None) -> str:
        """One key per tenant and theme, holding that theme's sections."""
        return f"web_sections:{tenant_id}:{theme_id or 'any'}"

    @staticmethod
    def invalidate(tenant_id: str, theme_id: Optional[UUID] = None) -> int:
        """Drop the cached sections after a section write."""
        return cache.delete(
            TenantWebsiteSectionsService.cache_key(tenant_id, theme_id)
        )

    @staticmethod
    def get_active_default_sections(
        db: Session,
        tenant_id: str,
        theme_id: Optional[UUID] = None,
    ) -> Optional[Any]:
        """
        The active theme's default sections, served from Redis.

        Concurrent misses each run the query: it is a single indexed lookup,
        so a fill lock would cost more than it saves. A tenant with no sections
        is not cached, so it is re-checked on the next request.
        """

        def loader() -> Optional[Any]:
            query = db.query(TenantWebsiteSection).filter(
                TenantWebsiteSection.tenant_id == tenant_id,
                TenantWebsiteSection.is_active.is_(True),
            )
            if theme_id is not None:
                query = query.filter(TenantWebsiteSection.theme_id == theme_id)

            row = query.first()
            return row.default_sections if row else None

        return cache.get_or_set(
            TenantWebsiteSectionsService.cache_key(tenant_id, theme_id),
            loader,
            ttl=CACHE_TTL,
        )
