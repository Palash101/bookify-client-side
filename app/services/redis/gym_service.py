from __future__ import annotations

from app.core.redis.cache import cache
from app.models.master_org import Organization
from app.schemas.tenant import TenantResponse


def _norm_domain(domain: str) -> str:
    return domain.strip().lower()


def gym_key(domain: str) -> str:
    """Payload for GET /gym, keyed by organization domain."""
    return f"gym:{_norm_domain(domain)}"


def domain_key(domain: str) -> str:
    """Existing map: hostname → organization_id (e.g. domain:nova.fitnezstudios.com)."""
    return f"domain:{_norm_domain(domain)}"


def org_cache_key(organization_id: str) -> str:
    """Existing org payload key (e.g. t:org-101)."""
    return f"t:{organization_id.strip().lower()}"


def _payload_from_tenant(tenant: Organization) -> dict:
    return {
        "organization_id": tenant.organization_id,
        "name": tenant.name,
        "domain": tenant.domain,
    }


def _from_org_cache(domain: str) -> TenantResponse | None:
    org_id = cache.get(domain_key(domain))
    if not org_id:
        return None
    payload = cache.get(org_cache_key(str(org_id)))
    if not payload:
        return None
    try:
        return TenantResponse.model_validate(payload)
    except Exception:
        return None


def get_or_cache_gym(tenant: Organization) -> TenantResponse:
    """
    Return gym details for this tenant's domain from Redis, or load from the
    tenant row, write the cache, and return.
    """
    domain = (tenant.domain or "").strip()
    if not domain:
        return TenantResponse.model_validate(tenant)

    cached = cache.get(gym_key(domain), TenantResponse)
    if cached is not None:
        return cached

    cached = _from_org_cache(domain)
    if cached is not None:
        cache.set(gym_key(domain), cached.model_dump(by_alias=True))
        return cached

    payload = _payload_from_tenant(tenant)
    cache.set(gym_key(domain), payload)
    cache.set(domain_key(domain), tenant.organization_id)
    return TenantResponse.model_validate(payload)
