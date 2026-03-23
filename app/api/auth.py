from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session
from app.core.db.session import get_db
from app.schemas.user import (
    UserCreate,
    Token,
    OTPRequest,
    OTPVerify,
    OTPResponse,
    PasswordResetRequest,
    PasswordResetVerify,
    PasswordResetResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    ProfileResponse,
    ProfileUpdate,
    MessageResponse,
)
from app.models.user import User as UserModel
from app.dependencies import get_current_tenant_id, get_current_active_user
from app.services.auth_service.auth_service import AuthService
import uuid

router = APIRouter()


@router.post("/login", response_model=OTPResponse)
async def login(
    user_credentials: OTPRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    User login endpoint - sends OTP to email.
    Tenant is resolved from X-Tenant-Key header.
    """
    AuthService.authenticate_user(db, user_credentials.email, user_credentials.password, tenant_id)
    otp_code, verification_token = await AuthService.send_otp(
        user_credentials.email, "login", tenant_id=tenant_id
    )
    
    return {
        "success": True,
        "message": "OTP sent to your email. Please verify to complete login.",
        "otp_code": otp_code,
        "token": verification_token
    }


@router.post("/register", response_model=OTPResponse)
async def register(
    user_data: UserCreate,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    User registration endpoint - validates data, sends OTP, but does NOT save user.
    User will be created only after OTP verification.
    """
    AuthService.validate_registration_data(user_data)
    AuthService.check_user_exists(db, user_data.email, tenant_id)
    
    user_data_dict = AuthService.prepare_registration_data(user_data, tenant_id)
    otp_code, verification_token = await AuthService.send_otp(
        user_data.email, "register", tenant_id=tenant_id, user_data=user_data_dict
    )
    
    return {
        "success": True,
        "message": "Registration successful. OTP sent to your email. Please verify to activate your account.",
        "otp_code": otp_code,
        "token": verification_token
    }


@router.post("/verify-otp", response_model=Token)
async def verify_otp_endpoint(
    otp_data: OTPVerify,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Verify OTP and return access token.
    Token is sent in Authorization header as "Bearer <token>".
    Works for both login and register OTPs.
    """
    authorization = request.headers.get("Authorization")
    email, otp_tenant_id = AuthService.extract_verification_context(authorization)
    purpose, cached_user_data = AuthService.verify_otp(
        email, otp_data.otp, otp_tenant_id=otp_tenant_id
    )

    if purpose == "register":
        if (
            cached_user_data
            and otp_tenant_id is not None
            and str(cached_user_data.get("tenant_id")) != str(otp_tenant_id)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Registration data does not match verification tenant.",
            )
        user = AuthService.create_user_from_cache(db, cached_user_data)
        message = "Registration successful. Your account has been created and activated."
    else:
        if otp_tenant_id is None:
            raise HTTPException(
                status_code=400,
                detail="Verification token missing tenant. Log in again: use X-Tenant-Key on /auth/login, then verify OTP with the new token.",
            )
        user = AuthService.get_user_for_login(db, email, otp_tenant_id)
        message = "Login successful. OTP verified."
    
    access_token, refresh_token = AuthService.generate_tokens(user)
    
    return {
        "success": True,
        "message": message,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": user,
    }


@router.post("/forgot-password", response_model=PasswordResetResponse)
async def forgot_password(
    reset_data: PasswordResetRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Forgot password - email bhejo, OTP email par aayega.
    User must exist and be active. Same as request-password-reset.
    """
    AuthService.get_user_for_login(db, reset_data.email, tenant_id)
    otp_code, verification_token = await AuthService.send_otp(
        reset_data.email, "password_reset", tenant_id=tenant_id
    )
    return {
        "success": True,
        "message": "OTP sent to your email. Please verify to reset your password.",
        "otp_code": otp_code,
        "token": verification_token,
    }


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    reset_data: PasswordResetVerify,
    request: Request,
    db: Session = Depends(get_db),
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
):
    """
    Reset password after OTP verification.
    Token is sent in Authorization header as "Bearer <token>".
    """
    authorization = request.headers.get("Authorization")
    email, otp_tenant_id = AuthService.extract_verification_context(authorization)
    AuthService.verify_otp(
        email,
        reset_data.otp,
        expected_purpose="password_reset",
        otp_tenant_id=otp_tenant_id,
    )
    if otp_tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification token missing tenant. Request password reset again with X-Tenant-Key.",
        )
    if otp_tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-Key does not match the tenant on your reset session.",
        )
    AuthService.reset_password(
        db,
        email,
        reset_data.new_password,
        reset_data.confirm_password,
        otp_tenant_id,
    )
    
    return {
        "success": True,
        "message": "Password reset successful. You can now login with your new password."
    }


@router.post("/refresh-token", response_model=RefreshTokenResponse)
async def refresh_token(
    token_data: RefreshTokenRequest,
    db: Session = Depends(get_db)
):
    """
    Refresh access token using refresh token.
    """
    new_access_token, new_refresh_token = AuthService.validate_and_refresh_token(db, token_data.refresh_token)
    
    return {
        "success": True,
        "message": "Token refreshed successfully",
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer"
    }


@router.get("/me", response_model=ProfileResponse)
async def get_me(
    current_user: UserModel = Depends(get_current_active_user),
):
    """
    Get current authenticated user (me).
    Requires authentication.
    """
    return {
        "success": True,
        "message": "Profile fetched successfully",
        "data": current_user
    }


@router.put("/me", response_model=ProfileResponse)
async def update_me(
    profile_data: ProfileUpdate,
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Update current authenticated user (me).
    Requires authentication.
    """
    updated_user = AuthService.update_profile(db, current_user, profile_data)
    return {
        "success": True,
        "message": "Profile updated successfully",
        "data": updated_user
    }
