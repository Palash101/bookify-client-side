from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session
from app.core.db.session import get_write_db
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
    UserMeResponse,
)
from app.models.user import User as UserModel
from app.dependencies import get_current_tenant_id, get_current_active_user
from app.services.auth_service.auth_service import AuthService
from app.core.settings import settings
import logging

router = APIRouter()
log = logging.getLogger(__name__)


def _otp_response(message: str, token: str, email: str, otp_code: str) -> dict:
    # TODO: remove otp_code from response before production (testing only).
    response = {
        "success": True,
        "message": message,
        "token": token,
        "otp_code": otp_code,
    }
    if settings.DEBUG:
        response["debug_otp_sent_to"] = email
    return response


@router.post("/login", response_model=OTPResponse)
async def login(
    user_credentials: OTPRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    db: Session = Depends(get_write_db),
):
    """
    User login endpoint - sends OTP to email.
    Tenant is resolved from X-Tenant-Key header.
    """
    user = AuthService.authenticate_user(
        db, user_credentials.email, user_credentials.password, tenant_id
    )
    log.info(
        "login_otp_request login_email=%s tenant_id=%s db_user_email=%s user_id=%s",
        user_credentials.email,
        tenant_id,
        user.email,
        user.id,
    )
    verification_token, otp_code = await AuthService.send_login_otp(user, tenant_id)

    return _otp_response(
        "OTP sent to your email. Please verify to complete login.",
        verification_token,
        user.email,
        otp_code,
    )


@router.post("/register", response_model=OTPResponse)
async def register(
    user_data: UserCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: Session = Depends(get_write_db),
):
    """
    User registration endpoint - validates data, sends OTP, but does NOT save user.
    User will be created only after OTP verification.
    """
    AuthService.validate_registration_data(user_data)
    AuthService.check_user_exists(db, user_data.email, tenant_id)
    
    user_data_dict = AuthService.prepare_registration_data(user_data, tenant_id)
    log.info(
        "register_otp_request email=%s tenant_id=%s",
        user_data.email,
        tenant_id,
    )
    verification_token, otp_code = await AuthService.send_register_otp(
        user_data.email,
        tenant_id,
        first_name=user_data.first_name,
        last_name=user_data.last_name,
        user_data=user_data_dict,
    )

    return _otp_response(
        "Registration successful. OTP sent to your email. Please verify to activate your account.",
        verification_token,
        user_data.email,
        otp_code,
    )


@router.post("/verify-otp", response_model=Token)
async def verify_otp_endpoint(
    otp_data: OTPVerify,
    request: Request,
    db: Session = Depends(get_write_db)
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
        "user": UserMeResponse.model_validate(user),
    }


@router.post("/resend-otp", response_model=OTPResponse)
async def resend_otp(
    request: Request,
    tenant_id: str = Depends(get_current_tenant_id),
    db: Session = Depends(get_write_db),
):
    """
    Resend OTP for login, register, or forgot-password flows.

    Send the verification Bearer token from /login, /register, or /forgot-password
    in the Authorization header. A new OTP is generated and emailed; the response
    includes a fresh verification token for /verify-otp or /reset-password.
    """
    authorization = request.headers.get("Authorization")
    verification_token, otp_code, email, purpose = await AuthService.resend_otp(
        db, authorization, tenant_id=tenant_id
    )

    if purpose == "register":
        message = (
            "OTP resent to your email. Please verify to activate your account."
        )
    elif purpose == "password_reset":
        message = "OTP resent to your email. Please verify to reset your password."
    else:
        message = "OTP resent to your email. Please verify to complete login."

    log.info(
        "resend_otp_request email=%s tenant_id=%s purpose=%s",
        email,
        tenant_id,
        purpose,
    )

    return _otp_response(message, verification_token, email, otp_code)


@router.post("/forgot-password", response_model=PasswordResetResponse)
async def forgot_password(
    reset_data: PasswordResetRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    db: Session = Depends(get_write_db),
):
    """
    Forgot password - email bhejo, OTP email par aayega.
    User must exist and be active. Same as request-password-reset.
    """
    user = AuthService.get_user_for_login(db, reset_data.email, tenant_id)
    log.info(
        "password_reset_otp_request email=%s tenant_id=%s",
        reset_data.email,
        tenant_id,
    )
    verification_token, otp_code = await AuthService.send_password_reset_otp(user, tenant_id)
    return _otp_response(
        "OTP sent to your email. Please verify to reset your password.",
        verification_token,
        reset_data.email,
        otp_code,
    )


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    reset_data: PasswordResetVerify,
    request: Request,
    db: Session = Depends(get_write_db),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """
    Two flows (send either otp OR old_password, not both):

    1) Forgot password (no old password):
       POST /forgot-password -> Bearer token from response + body: otp, new_password, confirm_password

    2) Change password (know current password):
       POST /login -> Bearer verification token (or access token after verify-otp)
       + body: old_password, new_password, confirm_password (no otp)
    """
    authorization = request.headers.get("Authorization")

    if reset_data.old_password:
        user = AuthService.resolve_user_from_bearer(db, authorization, tenant_id)
        AuthService.change_password_with_old(
            db,
            user,
            reset_data.old_password,
            reset_data.new_password,
            reset_data.confirm_password,
        )
        return {
            "success": True,
            "message": "Password changed successfully.",
        }

    AuthService.assert_forgot_password_verification_token(authorization)
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
        "message": "Password reset successful. You can now login with your new password.",
    }


@router.post("/refresh-token", response_model=RefreshTokenResponse)
async def refresh_token(
    token_data: RefreshTokenRequest,
    db: Session = Depends(get_write_db)
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
    db: Session = Depends(get_write_db)
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
