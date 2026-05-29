from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError, OperationalError

from app.models.tenant_setting import TenantSetting
from app.schemas.gym_config_value import GymConfigValue

GYM_CONFIG_KEY = "gym_config"


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
