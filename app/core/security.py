from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from app.core.settings import settings


def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    """
    Verify a password against a hash using bcrypt directly.
    """
    if not hashed_password:
        return False
    
    try:
        # Ensure password is bytes
        password_bytes = plain_password.encode('utf-8')
        hash_bytes = hashed_password.encode('utf-8')
        
        # Verify password
        return bcrypt.checkpw(password_bytes, hash_bytes)
    except (ValueError, TypeError, Exception) as e:
        # Handle bcrypt errors (e.g., password too long, invalid hash)
        return False


def get_password_hash(password: str) -> str:
    """
    Hash a password using bcrypt directly.
    """
    # Generate salt and hash password
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a JWT access token.
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def verify_token(token: str) -> Optional[dict]:
    """
    Verify and decode a JWT token.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        return None


def create_verification_token(
    email: str,
    purpose: str,
    tenant_id: Optional[str] = None,
    expiry_minutes: int = 10,
) -> str:
    """
    Create a temporary verification token containing email, purpose, and optional tenant_id.
    tenant_id ties OTP to one gym when the same email exists on multiple tenants.
    """
    to_encode: dict = {
        "email": email,
        "purpose": purpose,
        "type": "verification",
    }
    if tenant_id:
        to_encode["tenant_id"] = tenant_id
    expire = datetime.utcnow() + timedelta(minutes=expiry_minutes)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def extract_verification_claims(token: str) -> Optional[dict]:
    """
    Decode verification JWT: email, purpose, tenant_id (if present).
    """
    payload = verify_token(token)
    if not payload or payload.get("type") != "verification":
        return None
    return {
        "email": payload.get("email"),
        "purpose": payload.get("purpose"),
        "tenant_id": payload.get("tenant_id"),
    }


def extract_email_from_token(token: str) -> Optional[str]:
    """
    Extract email from verification token.
    """
    claims = extract_verification_claims(token)
    return claims.get("email") if claims else None


def create_refresh_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a JWT refresh token with longer expiry.
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    
    to_encode.update({"exp": expire, "type": "refresh"})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def verify_refresh_token(token: str) -> Optional[dict]:
    """
    Verify and decode a refresh token.
    """
    payload = verify_token(token)
    if payload and payload.get("type") == "refresh":
        return payload
    return None


def create_class_checkin_token(
    *,
    booking_id: str,
    class_id: str,
    tenant_id: str,
    user_id: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a signed token used inside class attendance QR codes.
    """
    to_encode = {
        "type": "class_checkin",
        "booking_id": booking_id,
        "class_id": class_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
    }
    expire = datetime.utcnow() + (
        expires_delta if expires_delta is not None else timedelta(hours=12)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def extract_class_checkin_claims(token: str) -> Optional[dict]:
    """
    Decode and validate a class attendance QR token.
    """
    payload = verify_token(token)
    if not payload or payload.get("type") != "class_checkin":
        return None
    return {
        "booking_id": payload.get("booking_id"),
        "class_id": payload.get("class_id"),
        "tenant_id": payload.get("tenant_id"),
        "user_id": payload.get("user_id"),
    }
