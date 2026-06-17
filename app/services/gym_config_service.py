from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError, OperationalError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.tenant_setting import TenantSetting
from app.schemas.gym_config_value import GymConfigValue

GYM_CONFIG_KEY = "gym_config"

# Map common DB abbreviations to IANA timezone names (zoneinfo does not accept "IST" etc.)
COMMON_TZ_ABBREVS = {
    "IST": "Asia/Kolkata",
    "GST": "Asia/Dubai",
    "QAT": "Asia/Qatar",
    "AST": "Asia/Riyadh",
    "PKT": "Asia/Karachi",
    "UTC": "UTC",
}


class GymConfigService:
    @staticmethod
    def get_gym_config(db: Session, tenant_id: str) -> GymConfigValue:
        try:
            row = (
                db.query(TenantSetting)
                .filter(
                    TenantSetting.tenant_id == tenant_id,
                    TenantSetting.setting_key == GYM_CONFIG_KEY,
                )
                .first()
            )
        except (ProgrammingError, OperationalError):
            # Settings table might not exist in some tenant DBs.
            try:
                db.rollback()
            except Exception:
                pass
            return GymConfigValue()
        if not row or row.value is None or row.is_enabled is False:
            return GymConfigValue()
        return GymConfigValue.from_json(row.value)

    @staticmethod
    def get_raw(db: Session, tenant_id: str) -> Optional[dict]:
        try:
            row = (
                db.query(TenantSetting)
                .filter(
                    TenantSetting.tenant_id == tenant_id,
                    TenantSetting.setting_key == GYM_CONFIG_KEY,
                )
                .first()
            )
        except (ProgrammingError, OperationalError):
            try:
                db.rollback()
            except Exception:
                pass
            return None
        if not row or row.value is None or row.is_enabled is False:
            return None
        if isinstance(row.value, dict):
            return row.value
        return None

    @staticmethod
    def get_currency(db: Session, tenant_id: str, default: str = "QAR") -> str:
        return GymConfigService.get_gym_config(db, tenant_id).resolved_currency(default)

    @staticmethod
    def resolve_zoneinfo(cfg: GymConfigValue) -> ZoneInfo:
        tz_name = cfg.resolved_timezone_name()
        tz_key = tz_name.upper()
        if tz_key in COMMON_TZ_ABBREVS:
            tz_name = COMMON_TZ_ABBREVS[tz_key]
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    @staticmethod
    def get_timezone(db: Session, tenant_id: str) -> ZoneInfo:
        return GymConfigService.resolve_zoneinfo(
            GymConfigService.get_gym_config(db, tenant_id)
        )
