from typing import Any, Optional
import json

from pydantic import BaseModel, ConfigDict, Field, AliasChoices


ORG_CONFIG_KEYS = (
    "organization_config",
    "organizationConfig",
    "organisation_config",
    "organisationConfig",
)

TIMEZONE_KEYS = (
    "timezone",
    "timeZone",
    "time_zone",
    "Timezone",
    "timeone",  # common typo in stored JSON
    "Timeone",
)


def _parse_json_mapping(raw: Any) -> Optional[dict]:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(raw, dict):
        return None
    return raw


def _extract_organization_config(raw: dict) -> dict:
    for key in ORG_CONFIG_KEYS:
        org = raw.get(key)
        if org is None:
            continue
        parsed = _parse_json_mapping(org)
        if parsed is not None:
            return parsed
    return {}


def _extract_timezone(org: dict) -> Optional[str]:
    for key in TIMEZONE_KEYS:
        value = org.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def normalize_gym_config_payload(raw: Any) -> Optional[dict]:
    """
    Normalize settings.gym_config.value into the shape expected by GymConfigValue.
    Handles camelCase keys, nested value/data wrappers, and JSON strings.
    """
    data = _parse_json_mapping(raw)
    if data is None:
        return None

    for wrapper_key in ("value", "data", "config"):
        wrapped = data.get(wrapper_key)
        if wrapped is not None:
            parsed = _parse_json_mapping(wrapped)
            if parsed is not None:
                data = parsed
                break

    org = _extract_organization_config(data)
    if org:
        normalized_org = dict(org)
        tz = _extract_timezone(org)
        if tz:
            normalized_org["timezone"] = tz
        currency = org.get("currency") or org.get("Currency")
        if currency is not None and str(currency).strip():
            normalized_org["currency"] = str(currency).strip()
        data = {**data, "organization_config": normalized_org}

    return data


class OrganizationConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    currency: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("currency", "Currency"),
    )
    timezone: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(*TIMEZONE_KEYS),
    )


class PaymentPricingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enable_free_classes: bool = False
    enable_class_package: bool = False
    enable_pay_per_class: bool = False


class BookingSettingsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    allow_waiting_list: bool = False
    auto_confirm_booking: bool = True
    allow_late_cancellations: bool = False
    cancellation_window_hours: int = 0
    advance_booking_window_days: int = 0
    booking_cutoff_minutes: int = 0


class AttendanceCheckInConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    no_show_penalty: Optional[str] = None
    auto_mark_no_shows: bool = False
    enable_qr_code_check_in: bool = False
    late_arrival_grace_period: bool = False


class ClassConfigurationConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    spot_reservation: bool = False
    default_class_capacity: Optional[int] = None
    default_class_duration: Optional[int] = None
    multiple_floor_layouts: bool = False
    enable_male_only_classes: bool = False
    enable_female_only_classes: bool = False


class NotificationSettingsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    class_reminder: bool = False
    waitlist_updates: bool = False
    booking_confirmation: bool = False
    birthday_notification: bool = False


class GymConfigValue(BaseModel):
    """
    Parsed gym_config JSON from public.settings.value.

    - Each known subsection ignores unknown keys inside it (safe partial / evolving JSON).
    - Root uses extra='allow' so new top-level sections survive parse + model_dump()
      (booking code can ignore them until supported).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    organization_config: OrganizationConfig = Field(
        default_factory=OrganizationConfig,
        validation_alias=AliasChoices(*ORG_CONFIG_KEYS),
    )
    payment_pricing: PaymentPricingConfig = Field(default_factory=PaymentPricingConfig)
    booking_settings: BookingSettingsConfig = Field(default_factory=BookingSettingsConfig)
    attendance_check_in: AttendanceCheckInConfig = Field(default_factory=AttendanceCheckInConfig)
    class_configuration: ClassConfigurationConfig = Field(default_factory=ClassConfigurationConfig)
    notification_settings: NotificationSettingsConfig = Field(
        default_factory=NotificationSettingsConfig
    )

    def resolved_currency(self, default: str = "QAR") -> str:
        raw = (self.organization_config.currency or "").strip()
        return raw.upper() if raw else default

    def resolved_timezone_name(self, default: str = "UTC") -> str:
        """Timezone from settings.gym_config.organization_config.timezone only."""
        raw = (self.organization_config.timezone or "").strip()
        return raw if raw else default

    @classmethod
    def from_json(cls, raw: Any) -> "GymConfigValue":
        normalized = normalize_gym_config_payload(raw)
        if not normalized:
            return cls()
        return cls.model_validate(normalized)
