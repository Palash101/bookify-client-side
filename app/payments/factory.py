"""
Payment Gateway Factory & Tenant Manager.

This module is the single entry-point for the rest of the application.
It is responsible for:
  1. Loading a tenant's active gateway configuration (from DB / settings).
  2. Instantiating the correct gateway class.
  3. Exposing a clean get_gateway() helper used by all FastAPI routes.
"""

from typing import Any, Optional, Union
import logging

from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError, OperationalError

from .base import BasePaymentGateway, GatewayType
from .stripe_gateway import StripePaymentGateway
from .paypal_gateway import PayPalPaymentGateway
from .myfatoorah_gateway import MyFatoorahPaymentGateway
from app.core.db.session import get_session_factory
from app.models.tenant_setting import TenantSetting

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Registry — add new gateways here only
# ---------------------------------------------------------------------------

GATEWAY_REGISTRY: dict[GatewayType, type[BasePaymentGateway]] = {
    GatewayType.STRIPE:     StripePaymentGateway,
    GatewayType.PAYPAL:     PayPalPaymentGateway,
    GatewayType.MYFATOORAH: MyFatoorahPaymentGateway,
}


# ---------------------------------------------------------------------------
# Tenant Settings Loader
# ---------------------------------------------------------------------------

class TenantPaymentSettings:
    """
    Loads and caches a tenant's payment configuration.

    DB schema uses the shared `settings` table, filtered by setting_key='payment_gateway'.
    The gateway type is read from value['type'], and the full value dict is the config.

        settings:
          tenant_id    str
          setting_key  str   -- must be 'payment_gateway'
          value        JSONB -- {"type": "stripe", ...provider config...}

    In-memory structure we expose from .get():

        {
          "tenant_id": "<uuid>",
          "active_gateway": "stripe",      # currently chosen default (first row or custom rule)
          "gateways": {
            "stripe": { ...payment_config... },
            "paypal": { ... },
            ...
          }
        }
    """

    # Simple in-process cache: tenant_id -> settings dict
    _cache: dict[str, dict[str, Any]] = {}

    @classmethod
    def get(cls, tenant_id: str) -> dict[str, Any]:
        if tenant_id not in cls._cache:
            cls._cache[tenant_id] = cls._load_from_db(tenant_id)
        return cls._cache[tenant_id]

    @classmethod
    def invalidate(cls, tenant_id: str) -> None:
        """Call this whenever a tenant updates their gateway config."""
        cls._cache.pop(tenant_id, None)

    @staticmethod
    def _load_from_db(tenant_id: str) -> dict[str, Any]:
        """
        Load tenant payment settings from the database.
        Returns the full tenant payment settings dict expected by the factory.
        """
        factory = get_session_factory(tenant_id)
        db: Session = factory()
        try:
            try:
                rows: list[TenantSetting] = (
                    db.query(TenantSetting)
                    .filter(
                        TenantSetting.tenant_id == tenant_id,
                        TenantSetting.setting_key == "payment_gateway",
                    )
                    .all()
                )
            except (ProgrammingError, OperationalError) as exc:
                logger.warning(
                    "Settings table unavailable (tenant_id=%s): %s",
                    tenant_id,
                    str(exc),
                )
                raise ValueError(
                    f"No payment settings configured for tenant '{tenant_id}'."
                ) from exc

            if not rows:
                raise ValueError(
                    f"No payment settings configured for tenant '{tenant_id}'."
                )

            gateways: dict[str, Any] = {}
            active_gateway: Optional[str] = None

            for row in rows:
                config = row.value or {}
                gt = (config.get("type") or "").lower()
                if not gt:
                    continue
                gateways[gt] = config
                # We'll decide active gateway after we load all rows.

            if not gateways:
                raise ValueError(
                    f"Tenant '{tenant_id}' has no gateway configurations."
                )

            # Deterministic default gateway:
            # - Prefer Stripe if configured (most common default)
            # - Else pick first gateway name sorted alphabetically
            if "stripe" in gateways:
                active_gateway = "stripe"
            else:
                active_gateway = sorted(gateways.keys())[0]

            return {
                "tenant_id": tenant_id,
                "active_gateway": active_gateway or "",
                "gateways": gateways,
            }
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Factory Function
# ---------------------------------------------------------------------------

def get_gateway(
    tenant_id: str,
    gateway_type: Optional[Union[GatewayType, str]] = None,
) -> BasePaymentGateway:
    """
    Resolve and instantiate the correct payment gateway for a tenant.

    Args:
        tenant_id:    The tenant requesting payment processing.
        gateway_type: Override the tenant's default. Accepts GatewayType enum
                      or its string value (e.g. "stripe").  If None, the
                      tenant's saved active_gateway is used.

    Returns:
        A ready-to-use BasePaymentGateway instance.

    Raises:
        ValueError: If the requested gateway is not configured for this tenant.
        KeyError:   If the gateway type is not in the registry.
    """
    tenant_settings = TenantPaymentSettings.get(tenant_id)

    # Resolve which gateway to use
    if gateway_type is None:
        raw = tenant_settings.get("active_gateway", "")
    elif isinstance(gateway_type, GatewayType):
        raw = gateway_type.value
    else:
        raw = str(gateway_type)

    try:
        resolved_type = GatewayType(raw.lower())
    except ValueError:
        raise ValueError(
            f"Unknown gateway type '{raw}'. "
            f"Valid options: {[g.value for g in GatewayType]}"
        )

    gateway_class = GATEWAY_REGISTRY.get(resolved_type)
    if gateway_class is None:
        raise KeyError(f"Gateway '{resolved_type}' is not registered.")

    gateway_configs = tenant_settings.get("gateways", {})
    gateway_config = gateway_configs.get(resolved_type.value)
    if not gateway_config:
        raise ValueError(
            f"Tenant '{tenant_id}' has no configuration for gateway '{resolved_type.value}'."
        )

    logger.info(
        "Loaded gateway '%s' for tenant '%s'", resolved_type.value, tenant_id
    )
    return gateway_class(settings=gateway_config)