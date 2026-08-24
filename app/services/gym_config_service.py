from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError, OperationalError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.tenant_setting import TenantSetting
from app.schemas.gym_config_value import GymConfigValue

GYM_CONFIG_KEY = "gym_config"

# Map common DB abbreviations / typos to IANA timezone names
COMMON_TZ_ABBREVS = {
    "IST": "Asia/Kolkata",
    "GST": "Asia/Dubai",
    "QAT": "Asia/Qatar",
    "AST": "Asia/Riyadh",
    "PKT": "Asia/Karachi",
    "UTC": "UTC",
    "ASIA/KOLKATTA": "Asia/Kolkata",  # common misspelling
}


class GymConfigService:
    @staticmethod
    def _fetch_gym_config_row(db: Session, tenant_id: str) -> Optional[TenantSetting]:
        def _ordered(query):
            return query.order_by(TenantSetting.updated_at.desc())

        key_match = func.lower(TenantSetting.setting_key) == GYM_CONFIG_KEY
        type_match = TenantSetting.value["type"].astext == GYM_CONFIG_KEY

        row = _ordered(
            db.query(TenantSetting).filter(
                key_match,
                TenantSetting.tenant_id == tenant_id,
            )
        ).first()
        if row:
            return row

        row = _ordered(
            db.query(TenantSetting).filter(
                type_match,
                TenantSetting.tenant_id == tenant_id,
            )
        ).first()
        if row:
            return row

        # Per-tenant DB may store a single gym_config row without tenant_id match.
        row = _ordered(db.query(TenantSetting).filter(key_match)).first()
        if row:
            return row

        return _ordered(db.query(TenantSetting).filter(type_match)).first()

    @staticmethod
    def get_gym_config(db: Session, tenant_id: str) -> GymConfigValue:
        try:
            row = GymConfigService._fetch_gym_config_row(db, tenant_id)
        except (ProgrammingError, OperationalError):
            # Settings table might not exist in some tenant DBs.
            try:
                db.rollback()
            except Exception:
                pass
            return GymConfigValue()
        if not row or row.value is None:
            return GymConfigValue()
        return GymConfigValue.from_json(row.value)

    @staticmethod
    def get_raw(db: Session, tenant_id: str) -> Optional[dict]:
        try:
            row = GymConfigService._fetch_gym_config_row(db, tenant_id)
        except (ProgrammingError, OperationalError):
            try:
                db.rollback()
            except Exception:
                pass
            return None
        if not row or row.value is None:
            return None
        parsed = GymConfigValue.from_json(row.value)
        dumped = parsed.model_dump()
        # Prefer original dict shape when available
        if isinstance(row.value, dict):
            return row.value
        return dumped if dumped else None

    @staticmethod
    def get_currency(db: Session, tenant_id: str, default: str = "QAR") -> str:
        return GymConfigService.get_gym_config(db, tenant_id).resolved_currency(default)

    @staticmethod
    def _normalize_timezone_name(tz_name: str, default: str = "UTC") -> str:
        raw = (tz_name or "").strip()
        if not raw:
            return default
        mapped = COMMON_TZ_ABBREVS.get(raw.upper())
        if mapped:
            return mapped
        try:
            ZoneInfo(raw)
            return raw
        except ZoneInfoNotFoundError:
            return default

    @staticmethod
    def get_timezone_name(db: Session, tenant_id: str, default: str = "UTC") -> str:
        """
        Timezone from settings where setting_key='gym_config':
        value.organization_config.timezone
        """
        cfg = GymConfigService.get_gym_config(db, tenant_id)
        tz_name = cfg.resolved_timezone_name(default="")
        return GymConfigService._normalize_timezone_name(tz_name, default=default)

    @staticmethod
    def resolve_zoneinfo(cfg: GymConfigValue, fallback_tz_name: Optional[str] = None) -> ZoneInfo:
        tz_name = cfg.resolved_timezone_name(default="")
        if not tz_name and fallback_tz_name:
            tz_name = fallback_tz_name
        tz_name = GymConfigService._normalize_timezone_name(tz_name or "")
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    @staticmethod
    def get_timezone(db: Session, tenant_id: str) -> ZoneInfo:
        tz_name = GymConfigService.get_timezone_name(db, tenant_id)
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")
