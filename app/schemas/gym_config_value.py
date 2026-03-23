from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class PaymentPricingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    currency: Optional[str] = None
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

    model_config = ConfigDict(extra="allow")

    payment_pricing: PaymentPricingConfig = Field(default_factory=PaymentPricingConfig)
    booking_settings: BookingSettingsConfig = Field(default_factory=BookingSettingsConfig)
    attendance_check_in: AttendanceCheckInConfig = Field(default_factory=AttendanceCheckInConfig)
    class_configuration: ClassConfigurationConfig = Field(default_factory=ClassConfigurationConfig)
    notification_settings: NotificationSettingsConfig = Field(
        default_factory=NotificationSettingsConfig
    )

    @classmethod
    def from_json(cls, raw: Any) -> "GymConfigValue":
        if raw is None or not isinstance(raw, dict):
            return cls()
        return cls.model_validate(raw)
