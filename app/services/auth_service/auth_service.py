from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from fastapi import HTTPException, status
from app.models.user import User, normalize_user_gender, user_gender_value
from app.models.role import Role
from app.core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    create_verification_token,
    extract_email_from_token,
    extract_verification_claims,
    create_refresh_token,
    verify_refresh_token,
    verify_token,
)
from app.core.otp_utils import create_otp, get_otp, verify_otp_any_purpose
from app.core.events.event_types import CLIENT_LOGIN_OTP
from app.services.event_publish_service import EventPublishService
from app.core.logging import get_logger
from app.schemas.user import UserCreate, ProfileUpdate
from datetime import datetime, timedelta, timezone, date as date_type
from app.core.settings import settings
from typing import Optional, Dict, Any, Tuple
import uuid

log = get_logger(__name__)

OTP_EXPIRY_MINUTES = 5

# Email consumer handles client.login_otp (same payload for all OTP flows).
CLIENT_OTP_EVENT_TYPE = CLIENT_LOGIN_OTP

CLIENT_OTP_EVENT_TYPES: Dict[str, str] = {
    "login": CLIENT_OTP_EVENT_TYPE,
    "register": CLIENT_OTP_EVENT_TYPE,
    "password_reset": CLIENT_OTP_EVENT_TYPE,
}


class AuthService:
    """
    Authentication service for user management.
    """
    
    @staticmethod
    def get_user_by_email(db: Session, email: str, tenant_id: str) -> Optional[User]:
        """
        Get user by email and tenant_id.
        """
        return (
            db.query(User)
            .filter(User.email == email, User.tenant_id == tenant_id)
            .first()
        )
    
    @staticmethod
    def get_user_by_id(db: Session, user_id: uuid.UUID) -> Optional[User]:
        """
        Get user by ID.
        """
        return db.query(User).filter(User.id == user_id).first()
    
    @staticmethod
    def authenticate_user(db: Session, email: str, password: str, tenant_id: str) -> User:
        """
        Authenticate a user by email and password.
        Raises HTTPException if authentication fails.
        """
        user = AuthService.get_user_by_email(db, email, tenant_id)
        
        if not user or not verify_password(password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your account is inactive. Please contact support to reactivate your account."
            )
        
        return user
    
    @staticmethod
    def check_user_exists(db: Session, email: str, tenant_id: str) -> None:
        """
        Check if user already exists. Raises HTTPException if exists.
        """
        existing_user = AuthService.get_user_by_email(db, email, tenant_id)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
    
    @staticmethod
    def validate_registration_data(user_data: UserCreate) -> None:
        """
        Validate registration data.
        """
        if user_data.password != user_data.confirm_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password and confirm password do not match"
            )
        
        if not user_data.terms_accepted:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You must accept the Terms & Conditions and Privacy Policy"
            )
    
    @staticmethod
    def prepare_registration_data(user_data: UserCreate, tenant_id: str) -> Dict[str, Any]:
        """
        Prepare user data dict for registration (to store in OTP cache).
        """
        password_hash = get_password_hash(user_data.password)
        
        phone_number = user_data.phone
        if user_data.phone_country_code and user_data.phone:
            phone_number = f"{user_data.phone_country_code}{user_data.phone}"
        
        role_id = str(user_data.role_id) if user_data.role_id else None

        return {
            "email": user_data.email,
            "password_hash": password_hash,
            "first_name": user_data.first_name,
            "last_name": user_data.last_name,
            "phone": phone_number,
            "gender": (
                normalize_user_gender(user_data.gender).value
                if normalize_user_gender(user_data.gender) is not None
                else None
            ),
            "dob": str(user_data.dob) if user_data.dob else None,
            "skills": None,
            "tenant_id": str(tenant_id),
            "role_id": role_id,
            # Mobile app registration creates clients by default
            "user_type": "client",
        }
    
    @staticmethod
    def _full_name(first_name: Optional[str], last_name: Optional[str], email: str) -> str:
        parts = [first_name, last_name]
        full_name = " ".join(part.strip() for part in parts if part and part.strip())
        return full_name or email

    @staticmethod
    def _user_full_name(user: User) -> str:
        return AuthService._full_name(user.first_name, user.last_name, user.email or "")

    @staticmethod
    def _pubsub_otp_error_detail(exc: Exception) -> str:
        detail = (
            "Unable to send OTP right now. "
            "Check GCP Pub/Sub topic, credentials, and that the email consumer is running."
        )
        exc_name = type(exc).__name__
        if exc_name == "PermissionDenied" or "PermissionDenied" in str(exc):
            detail = (
                f"GCP Pub/Sub permission denied for topic '{settings.PUBSUB_TOPIC_ID}'. "
                "Grant pubsub.topics.publish to the account in GOOGLE_APPLICATION_CREDENTIALS "
                f"(project: {settings.GCP_PROJECT_ID})."
            )
        elif exc_name == "NotFound" or "not found" in str(exc).lower():
            detail = (
                f"GCP Pub/Sub topic '{settings.PUBSUB_TOPIC_ID}' not found in "
                f"project '{settings.GCP_PROJECT_ID}'."
            )
        return detail

    @staticmethod
    async def send_client_otp(
        *,
        email: str,
        purpose: str,
        tenant_id: str,
        full_name: str,
        user_data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        """
        Generate OTP, publish a client OTP event via GCP Pub/Sub.
        Returns (verification_token, otp_code).
        """
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email is required to send OTP",
            )

        event_type = CLIENT_OTP_EVENT_TYPES.get(purpose)
        if not event_type:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unsupported OTP purpose: {purpose}",
            )

        tid_str = str(tenant_id)
        otp_code = create_otp(
            email,
            purpose,
            expiry_minutes=OTP_EXPIRY_MINUTES,
            user_data=user_data,
            tenant_id=tid_str,
        )

        event_data = {
            "to": email,
            "html": "",
            "text": "",
            "full_name": full_name,
            "otp": otp_code,
            "expiry_minutes": f"{OTP_EXPIRY_MINUTES}Min",
        }
        log.info(
            "client_otp_prepare purpose=%s event_type=%s tenant_id=%s to_email=%s "
            "full_name=%s otp=%s topic=%s/%s",
            purpose,
            event_type,
            tid_str,
            email,
            full_name,
            otp_code,
            settings.GCP_PROJECT_ID,
            settings.PUBSUB_TOPIC_ID,
        )

        try:
            published = await EventPublishService.publish(
                tenant_id=tid_str,
                event_type=event_type,
                data=event_data,
                ordering_key=tid_str,
            )
            log.info(
                "client_otp_published purpose=%s event_type=%s to_email=%s tenant_id=%s "
                "event_id=%s message_id=%s",
                purpose,
                event_type,
                email,
                tid_str,
                published.event_id,
                published.message_id,
            )
        except Exception as exc:
            log.exception(
                "%s publish failed for tenant=%s email=%s", event_type, tid_str, email
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=AuthService._pubsub_otp_error_detail(exc),
            ) from exc

        token = create_verification_token(email, purpose, tenant_id=tid_str)
        return token, otp_code

    @staticmethod
    async def send_login_otp(user: User, tenant_id: str) -> Tuple[str, str]:
        return await AuthService.send_client_otp(
            email=user.email,
            purpose="login",
            tenant_id=tenant_id,
            full_name=AuthService._user_full_name(user),
        )

    @staticmethod
    async def send_register_otp(
        email: str,
        tenant_id: str,
        *,
        first_name: Optional[str],
        last_name: Optional[str],
        user_data: Dict[str, Any],
    ) -> Tuple[str, str]:
        return await AuthService.send_client_otp(
            email=email,
            purpose="register",
            tenant_id=tenant_id,
            full_name=AuthService._full_name(first_name, last_name, email),
            user_data=user_data,
        )

    @staticmethod
    async def send_password_reset_otp(user: User, tenant_id: str) -> Tuple[str, str]:
        return await AuthService.send_client_otp(
            email=user.email,
            purpose="password_reset",
            tenant_id=tenant_id,
            full_name=AuthService._user_full_name(user),
        )
    
    @staticmethod
    def extract_and_validate_token(authorization: Optional[str]) -> str:
        """
        Extract email from Authorization header token.
        """
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization header missing",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        try:
            scheme, token = authorization.split()
            if scheme.lower() != "bearer":
                raise ValueError("Invalid scheme")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header format. Use 'Bearer <token>'",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        email = extract_email_from_token(token)
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired verification token"
            )
        
        return email

    @staticmethod
    def _parse_bearer_token(authorization: Optional[str]) -> str:
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization header missing",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            scheme, token = authorization.split()
            if scheme.lower() != "bearer":
                raise ValueError("Invalid scheme")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header format. Use 'Bearer <token>'",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return token

    @staticmethod
    def extract_verification_context(authorization: Optional[str]) -> Tuple[str, Optional[str]]:
        """
        Email + tenant_id from Bearer verification JWT (OTP flow).
        """
        email, otp_tenant_id, _purpose = AuthService.extract_verification_session(
            authorization
        )
        return email, otp_tenant_id

    @staticmethod
    def extract_verification_session(
        authorization: Optional[str],
    ) -> Tuple[str, Optional[str], str]:
        """
        Email, tenant_id, and purpose from Bearer verification JWT (OTP flow).
        """
        token = AuthService._parse_bearer_token(authorization)
        claims = extract_verification_claims(token)
        if not claims or not claims.get("email"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired verification token",
            )
        purpose = claims.get("purpose")
        if purpose not in CLIENT_OTP_EVENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired verification token",
            )
        email = claims["email"]
        tid_raw = claims.get("tenant_id")
        otp_tenant_id: Optional[str] = None
        if tid_raw:
            otp_tenant_id = str(tid_raw)
        return email, otp_tenant_id, purpose

    @staticmethod
    async def resend_otp(
        db: Session,
        authorization: Optional[str],
        tenant_id: Optional[str] = None,
    ) -> Tuple[str, str, str, str]:
        """
        Resend OTP using the verification Bearer token from login/register/forgot-password.
        Returns (verification_token, otp_code, email, purpose).
        """
        email, otp_tenant_id, purpose = AuthService.extract_verification_session(
            authorization
        )
        effective_tenant = otp_tenant_id or tenant_id
        if not effective_tenant:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Verification token missing tenant. Request OTP again with "
                    "X-Tenant-Key on login, register, or forgot-password."
                ),
            )
        if (
            tenant_id is not None
            and otp_tenant_id is not None
            and otp_tenant_id != tenant_id
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Tenant-Key does not match the tenant on your verification session.",
            )

        user_data: Optional[Dict[str, Any]] = None
        if purpose == "register":
            cached = get_otp(email, purpose, tenant_id=effective_tenant)
            if not cached or not cached.get("user_data"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Registration session expired. Please register again.",
                )
            user_data = cached["user_data"]
            full_name = AuthService._full_name(
                user_data.get("first_name"),
                user_data.get("last_name"),
                email,
            )
        else:
            user = AuthService.get_user_for_login(db, email, effective_tenant)
            full_name = AuthService._user_full_name(user)

        token, otp_code = await AuthService.send_client_otp(
            email=email,
            purpose=purpose,
            tenant_id=effective_tenant,
            full_name=full_name,
            user_data=user_data,
        )
        return token, otp_code, email, purpose

    @staticmethod
    def assert_forgot_password_verification_token(authorization: Optional[str]) -> None:
        """
        Ensure Bearer token was issued by /forgot-password (not /login).
        """
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization header missing",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            scheme, token = authorization.split()
            if scheme.lower() != "bearer":
                raise ValueError("Invalid scheme")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header format. Use 'Bearer <token>'",
                headers={"WWW-Authenticate": "Bearer"},
            )
        claims = extract_verification_claims(token)
        if not claims or claims.get("purpose") != "password_reset":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Invalid reset session. Call POST /forgot-password and use the token from that "
                    "response with the OTP from your email."
                ),
            )
    
    @staticmethod
    def verify_otp(
        email: str,
        otp: str,
        expected_purpose: Optional[str] = None,
        otp_tenant_id: Optional[str] = None,
    ) -> Tuple[str, Optional[Dict]]:
        """
        Verify OTP and return (purpose, cached_user_data).
        """
        tid_str = str(otp_tenant_id) if otp_tenant_id else None
        is_valid, purpose, cached_user_data = verify_otp_any_purpose(
            email, otp, tenant_id=tid_str
        )
        
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired OTP"
            )
        
        if expected_purpose and purpose != expected_purpose:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired OTP"
            )
        
        return purpose, cached_user_data
    
    @staticmethod
    def create_user_from_cache(db: Session, cached_user_data: Dict[str, Any]) -> User:
        """
        Create user from cached registration data.
        Note: User existence check is already done in register API, no need to check again here.
        """
        if not cached_user_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User data not found. Please register again."
            )
        
        dob = None
        if cached_user_data.get("dob"):
            dob = date_type.fromisoformat(cached_user_data["dob"])

        role_id = cached_user_data.get("role_id")
        if role_id:
            role_id = uuid.UUID(role_id) if isinstance(role_id, str) else role_id
        else:
            default_role = db.query(Role).filter(Role.key == "user").first()
            if not default_role:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Default role (key='user') not found. Please contact admin.",
                )
            role_id = default_role.id

        db_user = User(
            email=cached_user_data["email"],
            password_hash=cached_user_data["password_hash"],
            first_name=cached_user_data["first_name"],
            last_name=cached_user_data["last_name"],
            phone=cached_user_data.get("phone"),
            gender=normalize_user_gender(cached_user_data.get("gender")),
            dob=dob,
            skills=cached_user_data.get("skills"),
            is_active=True,
            tenant_id=cached_user_data["tenant_id"],
            role_id=role_id,
            # If somehow user_type not present in cache, treat as client for app
            user_type=cached_user_data.get("user_type", "client"),
        )
        
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        return db_user
    
    @staticmethod
    def get_user_for_login(db: Session, email: str, tenant_id: Optional[str] = None) -> User:
        """
        Get user for login flow (after OTP verification).
        If tenant_id is provided, ensure we fetch user for that tenant only.
        """
        query = db.query(User).filter(User.email == email)
        if tenant_id:
            query = query.filter(User.tenant_id == tenant_id)
        user = query.first()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User account is not active"
            )
        
        return user
    
    @staticmethod
    def generate_tokens(user: User) -> Tuple[str, str]:
        """
        Generate access token and refresh token for user.
        """
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={
                "sub": str(user.id),
                "email": user.email,
                "tenant_id": str(user.tenant_id),
            },
            expires_delta=access_token_expires,
        )
        refresh_token = create_refresh_token(
            data={
                "sub": str(user.id),
                "email": user.email,
                "tenant_id": str(user.tenant_id),
            }
        )
        return access_token, refresh_token
    
    @staticmethod
    def validate_and_refresh_token(db: Session, refresh_token_str: str) -> Tuple[str, str]:
        """
        Validate refresh token and generate new tokens.
        """
        payload = verify_refresh_token(refresh_token_str)
        
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload"
            )
        
        user = db.query(User).filter(User.id == uuid.UUID(user_id)).first()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        tid = payload.get("tenant_id")
        if tid is not None:
            if str(tid) != str(user.tenant_id):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Refresh token tenant mismatch",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User account is not active"
            )
        
        return AuthService.generate_tokens(user)
    
    @staticmethod
    def reset_password(
        db: Session,
        email: str,
        new_password: str,
        confirm_password: str,
        tenant_id: Optional[str] = None,
    ) -> None:
        """
        Set a new password (forgot-password OTP flow or after old password verified).
        """
        if new_password != confirm_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password and confirm password do not match"
            )
        
        query = db.query(User).filter(User.email == email)
        if tenant_id:
            query = query.filter(User.tenant_id == tenant_id)
        user = query.first()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        user.password_hash = get_password_hash(new_password)
        db.commit()

    @staticmethod
    def change_password_with_old(
        db: Session,
        user: User,
        old_password: str,
        new_password: str,
        confirm_password: str,
    ) -> None:
        """
        Change password when the user knows their current password.
        """
        if not verify_password(old_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            )
        AuthService.reset_password(
            db,
            user.email,
            new_password,
            confirm_password,
            user.tenant_id,
        )

    @staticmethod
    def resolve_user_from_bearer(
        db: Session,
        authorization: Optional[str],
        tenant_id: str,
    ) -> User:
        """
        Resolve user from access JWT or login verification JWT (after /login, before OTP).
        """
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization header missing",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            scheme, token = authorization.split()
            if scheme.lower() != "bearer":
                raise ValueError("Invalid scheme")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header format. Use 'Bearer <token>'",
                headers={"WWW-Authenticate": "Bearer"},
            )

        claims = extract_verification_claims(token)
        if claims and claims.get("email"):
            token_purpose = claims.get("purpose")
            if token_purpose not in ("login", None):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Use your login or access token to change password with old_password.",
                )
            otp_tenant_id = None
            tid_raw = claims.get("tenant_id")
            if tid_raw:
                otp_tenant_id = str(tid_raw)
            if otp_tenant_id is not None and otp_tenant_id != tenant_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="X-Tenant-Key does not match your session tenant.",
                )
            effective_tenant = otp_tenant_id or tenant_id
            return AuthService.get_user_for_login(db, claims["email"], effective_tenant)

        payload = verify_token(token)
        if payload and payload.get("sub") and payload.get("type") not in ("verification", "refresh"):
            try:
                user_id = uuid.UUID(str(payload["sub"]))
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid access token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found",
                )
            tid_claim = payload.get("tenant_id")
            if tid_claim is not None:
                if str(tid_claim) != str(user.tenant_id):
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Access token tenant mismatch",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            if user.tenant_id != tenant_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="X-Tenant-Key does not match the user tenant.",
                )
            if not user.is_active:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="User account is not active",
                )
            return user

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Log in or use forgot-password first.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    @staticmethod
    def update_profile(db: Session, user: User, profile_data: ProfileUpdate) -> User:
        """
        Update user profile.
        """
        # Ensure the user instance is attached to the current DB session
        user = db.merge(user)
        update_data = profile_data.model_dump(exclude_unset=True)
        
        if "phone_country_code" in update_data and "phone" in update_data:
            if update_data.get("phone_country_code") and update_data.get("phone"):
                update_data["phone"] = f"{update_data['phone_country_code']}{update_data['phone']}"
            del update_data["phone_country_code"]
        elif "phone_country_code" in update_data:
            del update_data["phone_country_code"]
        
        if "nationality" in update_data:
            current_skills = user.skills or {}
            current_skills["nationality"] = update_data["nationality"]
            update_data["skills"] = current_skills
            del update_data["nationality"]
        
        for field, value in update_data.items():
            if hasattr(user, field):
                setattr(user, field, value)
        
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def deactivate_account(db: Session, user: User, reason: str) -> User:
        """
        Soft-delete / deactivate the current user account.
        Stores deletion_reason + deactivated_at on dedicated user columns.
        """
        reason_text = (reason or "").strip()
        if not reason_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reason is required to delete your account.",
            )

        user = db.merge(user)
        if user.is_active is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your account is already deactivated.",
            )

        # Clean up earlier mistaken write into skills JSONB (if present).
        if isinstance(user.skills, dict):
            skills = dict(user.skills)
            skills.pop("deletion_reason", None)
            skills.pop("deactivated_at", None)
            user.skills = skills or None
            flag_modified(user, "skills")

        user.is_active = False
        user.deletion_reason = reason_text
        user.deactivated_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(user)
        log.info(
            "account_deactivated user_id=%s tenant_id=%s",
            user.id,
            user.tenant_id,
        )
        return user
