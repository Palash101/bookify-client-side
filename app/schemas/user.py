from pydantic import BaseModel, EmailStr
from typing import Optional, Any
from datetime import datetime, date
from uuid import UUID


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
    gender: Optional[str] = None  # "MALE" or "FEMALE"
    password: str
    confirm_password: str
    terms_accepted: bool = False
    
    # Optional fields
    tenant_id: Optional[UUID] = None
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
                "gender": "MALE",
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
    tenant_id: UUID
    role_id: UUID
    wallet: Optional[float] = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class User(UserInDB):
    pass


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    success: bool = True
    message: str
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str
    user: Optional[User] = None  # Logged-in user details (after verify-otp)


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


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetVerify(BaseModel):
    otp: str
    new_password: str
    confirm_password: str


class PasswordResetResponse(BaseModel):
    success: bool = True
    message: str
    otp_code: Optional[str] = None  # For testing only, remove in production
    token: str


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
    data: User


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
    
    class Config:
        json_schema_extra = {
            "example": {
                "first_name": "John",
                "last_name": "Doe",
                "phone": "12345678",
                "phone_country_code": "+974",
                "gender": "MALE",
                "dob": "1990-01-01",
                "nationality": "Qatar"
            }
        }


class MessageResponse(BaseModel):
    success: bool = True
    message: str
