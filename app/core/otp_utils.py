import random
from datetime import datetime, timedelta
from typing import Tuple, Optional, Dict
from app.core.otp_cache import otp_cache


def generate_otp() -> str:
    """
    Generate a 6-digit OTP.
    """
    return str(random.randint(100000, 999999))


def create_otp(
    email: str,
    purpose: str,
    expiry_minutes: int = 10,
    user_data: Optional[Dict] = None,
    tenant_id: Optional[str] = None,
) -> str:
    """
    Create and store an OTP in session cache.
    
    Args:
        email: User email
        purpose: Purpose of OTP ('login' or 'register')
        expiry_minutes: OTP expiry time in minutes (default: 10)
        user_data: Optional user data to store (for registration)
    
    Returns:
        str: Generated OTP code
    """
    # Generate new OTP
    otp_code = generate_otp()
    
    # Store in cache (automatically replaces existing OTP for same email+purpose)
    otp_cache.store_otp(
        email, purpose, otp_code, expiry_minutes, user_data, tenant_id=tenant_id
    )
    
    return otp_code


def verify_otp(
    email: str, otp_code: str, purpose: str, tenant_id: Optional[str] = None
) -> bool:
    """
    Verify an OTP from session cache.
    
    Args:
        email: User email
        otp_code: OTP code to verify
        purpose: Purpose of OTP ('login' or 'register')
    
    Returns:
        bool: True if OTP is valid, False otherwise
    """
    return otp_cache.verify_otp(email, otp_code, purpose, tenant_id=tenant_id)


def verify_otp_any_purpose(
    email: str, otp_code: str, tenant_id: Optional[str] = None
) -> Tuple[bool, Optional[str], Optional[Dict]]:
    """
    Verify an OTP for any purpose (login or register) from session cache.
    
    Args:
        email: User email
        otp_code: OTP code to verify
    
    Returns:
        tuple: (is_valid, purpose, user_data) where purpose is 'login' or 'register' or None,
               and user_data is stored user data (for registration) or None
    """
    return otp_cache.verify_otp_any_purpose(email, otp_code, tenant_id=tenant_id)
