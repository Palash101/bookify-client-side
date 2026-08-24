from pydantic import BaseModel, EmailStr, field_validator, model_validator
from typing import Optional, Any
from datetime import datetime, date
from uuid import UUID

from app.models.user import normalize_user_gender, user_gender_value


class UserBase(BaseModel):
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None  # For backward compatibility
    avatar: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[date] = None
    designation: Optional[str] = None
    # JSONB: may be {}, [] or null in DB
    skills: Optional[Any] = None
    is_active: bool = True


class UserCreate(UserBase):
    # Required fields for registration
    first_name: str
    last_name: str
    email: EmailStr
    phone: Optional[str] = None
    phone_country_code: Optional[str] = None  # e.g., "+974"
    dob: Optional[date] = None
    gender: Optional[str] = None  # "male" or "female"
    password: str
    confirm_password: str
    terms_accepted: bool = False
    
    # Optional fields
    tenant_id: Optional[str] = None
    role_id: Optional[UUID] = None
    
    class Config:
        json_schema_extra = {
            "example": {
                "first_name": "John",
                "last_name": "Doe",
                "email": "john.doe@example.com",
                "phone": "12345678",
                "phone_country_code": "+974",
                "dob": "1990-01-01",
                "gender": "male",
                "password": "SecurePassword123",
                "confirm_password": "SecurePassword123",
                "terms_accepted": True
            }
        }


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    avatar: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[date] = None
    designation: Optional[str] = None
    skills: Optional[Any] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


class UserInDB(UserBase):
    id: UUID
    tenant_id: str
    role_id: UUID
    wallet: Optional[float] = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class User(UserInDB):
    pass


class UserMeResponse(BaseModel):
    """
    Safe user payload for client `GET /auth/me`.
    Avoids exposing internal fields (tenant_id, role_id, is_active, timestamps).
    """
    id: UUID
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    avatar: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[date] = None
    designation: Optional[str] = None
    skills: Optional[Any] = None
    wallet: Optional[float] = 0

    class Config:
        from_attributes = True

    @field_validator("gender", mode="before")
    @classmethod
    def coerce_gender(cls, value: Any) -> Optional[str]:
        return user_gender_value(value)

    @field_validator("wallet", mode="before")
    @classmethod
    def coerce_wallet(cls, value: Any) -> float:
        if value is None:
            return 0
        return float(value)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    success: bool = True
    message: str
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str
    user: Optional[UserMeResponse] = None  # Logged-in user details (after verify-otp)


class TokenData(BaseModel):
    email: Optional[str] = None


class OTPRequest(BaseModel):
    email: EmailStr
    password: str


class OTPVerify(BaseModel):
    otp: str


class OTPResponse(BaseModel):
    success: bool = True
    message: str
    otp_code: Optional[str] = None  # OTP code (for testing only, remove in production)
    token: str  # Verification token containing email, to be used in verify-otp
    debug_otp_sent_to: Optional[str] = None  # Populated when DEBUG=true (login Pub/Sub target)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetVerify(BaseModel):
    """
    Forgot password: send otp only (Bearer token from /forgot-password).
    Change password (logged in / after login): send old_password only (Bearer access or login verification token).
    """
    otp: Optional[str] = None
    old_password: Optional[str] = None
    new_password: str
    confirm_password: str

    @model_validator(mode="after")
    def require_otp_or_old_password(self):
        has_otp = bool(self.otp and str(self.otp).strip())
        has_old = bool(self.old_password and str(self.old_password).strip())
        if has_otp and has_old:
            raise ValueError(
                "Send either otp (forgot-password flow) or old_password (change-password flow), not both."
            )
        if not has_otp and not has_old:
            raise ValueError(
                "Send otp for forgot-password, or old_password if you know your current password."
            )
        return self


class PasswordResetResponse(BaseModel):
    success: bool = True
    message: str
    otp_code: Optional[str] = None  # For testing only, remove in production
    token: str
    debug_otp_sent_to: Optional[str] = None  # Populated when DEBUG=true


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class RefreshTokenResponse(BaseModel):
    success: bool = True
    message: str
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class ProfileResponse(BaseModel):
    success: bool = True
    message: str
    data: UserMeResponse


class ProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    phone_country_code: Optional[str] = None
    gender: Optional[str] = None
    dob: Optional[date] = None
    avatar: Optional[str] = None
    designation: Optional[str] = None
    nationality: Optional[str] = None

    @field_validator("gender")
    @classmethod
    def coerce_gender(cls, value: Optional[str]) -> Optional[str]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        normalized = normalize_user_gender(value)
        if normalized is None:
            raise ValueError("Invalid gender. Allowed values: male, female")
        return normalized.value
    
    class Config:
        json_schema_extra = {
            "example": {
                "first_name": "John",
                "last_name": "Doe",
                "phone": "12345678",
                "phone_country_code": "+974",
                "gender": "male",
                "dob": "1990-01-01",
                "nationality": "Qatar"
            }
        }


class DeleteAccountRequest(BaseModel):
    reason: str

    class Config:
        json_schema_extra = {
            "example": {
                "reason": "No longer using the app",
            }
        }


class MessageResponse(BaseModel):
    success: bool = True
    message: str
