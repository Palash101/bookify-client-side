from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from app.models.user import User
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
)
from app.core.otp_utils import create_otp, verify_otp_any_purpose
from app.core.mailer import email_service
from app.schemas.user import UserCreate, ProfileUpdate
from datetime import timedelta, date as date_type
from app.core.settings import settings
from typing import Optional, Dict, Any, Tuple
import uuid


class AuthService:
    """
    Authentication service for user management.
    """
    
    @staticmethod
    def get_user_by_email(db: Session, email: str, tenant_id: uuid.UUID) -> Optional[User]:
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
    def authenticate_user(db: Session, email: str, password: str, tenant_id: uuid.UUID) -> User:
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
                detail="Inactive user"
            )
        
        return user
    
    @staticmethod
    def check_user_exists(db: Session, email: str, tenant_id: uuid.UUID) -> None:
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
    def prepare_registration_data(user_data: UserCreate, tenant_id: uuid.UUID) -> Dict[str, Any]:
        """
        Prepare user data dict for registration (to store in OTP cache).
        """
        password_hash = get_password_hash(user_data.password)
        
        phone_number = user_data.phone
        if user_data.phone_country_code and user_data.phone:
            phone_number = f"{user_data.phone_country_code}{user_data.phone}"
        
        skills_data = {}
        if user_data.nationality:
            skills_data["nationality"] = user_data.nationality

        role_id = str(user_data.role_id) if user_data.role_id else None

        return {
            "email": user_data.email,
            "password_hash": password_hash,
            "first_name": user_data.first_name,
            "last_name": user_data.last_name,
            "phone": phone_number,
            "gender": user_data.gender,
            "dob": str(user_data.dob) if user_data.dob else None,
            "skills": skills_data if skills_data else None,
            "tenant_id": str(tenant_id),
            "role_id": role_id,
            # Mobile app registration creates clients by default
            "user_type": "client",
        }
    
    @staticmethod
    async def send_otp(
        email: str,
        purpose: str,
        tenant_id: Optional[uuid.UUID] = None,
        user_data: Optional[Dict] = None,
    ) -> Tuple[str, str]:
        """
        Generate OTP, send email, and return (otp_code, verification_token).
        tenant_id scopes OTP + verification JWT to one gym (same email on multiple tenants).
        """
        tid_str: Optional[str] = None
        if tenant_id is not None:
            tid_str = str(tenant_id)
        elif user_data and user_data.get("tenant_id"):
            tid_str = str(user_data["tenant_id"])
        otp_code = create_otp(email, purpose, user_data=user_data, tenant_id=tid_str)
        await email_service.send_otp_email(email, otp_code, purpose)
        verification_token = create_verification_token(email, purpose, tenant_id=tid_str)
        return otp_code, verification_token
    
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
    def extract_verification_context(authorization: Optional[str]) -> Tuple[str, Optional[uuid.UUID]]:
        """
        Email + tenant_id from Bearer verification JWT (OTP flow).
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
        if not claims or not claims.get("email"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired verification token",
            )
        email = claims["email"]
        tid_raw = claims.get("tenant_id")
        otp_tenant_id: Optional[uuid.UUID] = None
        if tid_raw:
            try:
                otp_tenant_id = uuid.UUID(str(tid_raw))
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid verification token (tenant_id)",
                )
        return email, otp_tenant_id
    
    @staticmethod
    def verify_otp(
        email: str,
        otp: str,
        expected_purpose: Optional[str] = None,
        otp_tenant_id: Optional[uuid.UUID] = None,
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
            gender=cached_user_data.get("gender"),
            dob=dob,
            skills=cached_user_data.get("skills"),
            is_active=True,
            tenant_id=uuid.UUID(cached_user_data["tenant_id"]),
            role_id=role_id,
            # If somehow user_type not present in cache, treat as client for app
            user_type=cached_user_data.get("user_type", "client"),
        )
        
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        return db_user
    
    @staticmethod
    def get_user_for_login(db: Session, email: str, tenant_id: Optional[uuid.UUID] = None) -> User:
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
            try:
                if uuid.UUID(str(tid)) != uuid.UUID(str(user.tenant_id)):
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Refresh token tenant mismatch",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid refresh token",
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
        tenant_id: Optional[uuid.UUID] = None,
    ) -> None:
        """
        Reset user password.
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
