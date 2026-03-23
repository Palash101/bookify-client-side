"""
In-memory OTP cache for storing OTPs in session instead of database.
"""
import random
from datetime import datetime, timedelta
from typing import Tuple, Optional, Dict
from threading import Lock
import time


class OTPCache:
    """
    Thread-safe in-memory cache for OTP storage.
    """
    
    def __init__(self):
        self._cache: Dict[str, dict] = {}
        self._lock = Lock()
        self._cleanup_interval = 300  # Cleanup every 5 minutes
        self._last_cleanup = time.time()
    
    def _get_key(self, email: str, purpose: str, tenant_id: Optional[str] = None) -> str:
        """
        Cache key. When tenant_id is set, same email on different gyms has separate OTPs.
        Legacy key (tenant_id None) kept for backward compatibility only.
        """
        if tenant_id:
            return f"{tenant_id}\x1f{email}\x1f{purpose}"
        return f"{email}:{purpose}"
    
    def _cleanup_expired(self):
        """Remove expired OTPs from cache."""
        current_time = datetime.utcnow()
        expired_keys = [
            key for key, value in self._cache.items()
            if value['expires_at'] < current_time
        ]
        for key in expired_keys:
            del self._cache[key]
    
    def _auto_cleanup(self):
        """Auto cleanup expired entries periodically."""
        current_time = time.time()
        if current_time - self._last_cleanup > self._cleanup_interval:
            self._cleanup_expired()
            self._last_cleanup = current_time
    
    def store_otp(
        self,
        email: str,
        purpose: str,
        otp_code: str,
        expiry_minutes: int = 10,
        user_data: Optional[Dict] = None,
        tenant_id: Optional[str] = None,
    ) -> None:
        """
        Store OTP in cache.
        
        Args:
            email: User email
            purpose: Purpose of OTP ('login' or 'register')
            otp_code: OTP code to store
            expiry_minutes: OTP expiry time in minutes (default: 10)
            user_data: Optional user data to store (for registration)
        """
        with self._lock:
            self._auto_cleanup()
            key = self._get_key(email, purpose, tenant_id)
            expires_at = datetime.utcnow() + timedelta(minutes=expiry_minutes)
            
            cache_data = {
                'otp_code': otp_code,
                'purpose': purpose,
                'expires_at': expires_at,
                'created_at': datetime.utcnow()
            }
            
            # Store user data if provided (for registration)
            if user_data:
                cache_data['user_data'] = user_data
            
            self._cache[key] = cache_data
    
    def get_otp(self, email: str, purpose: str, tenant_id: Optional[str] = None) -> Optional[dict]:
        """
        Get OTP from cache.
        
        Args:
            email: User email
            purpose: Purpose of OTP ('login' or 'register')
        
        Returns:
            dict with OTP info if found and not expired, None otherwise
        """
        with self._lock:
            self._auto_cleanup()
            key = self._get_key(email, purpose, tenant_id)
            
            if key not in self._cache:
                return None
            
            otp_data = self._cache[key]
            
            # Check if expired
            if otp_data['expires_at'] < datetime.utcnow():
                del self._cache[key]
                return None
            
            return otp_data
    
    def verify_otp(
        self, email: str, otp_code: str, purpose: str, tenant_id: Optional[str] = None
    ) -> bool:
        """
        Verify OTP and remove it if valid.
        
        Args:
            email: User email
            otp_code: OTP code to verify
            purpose: Purpose of OTP ('login' or 'register')
        
        Returns:
            bool: True if OTP is valid, False otherwise
        """
        with self._lock:
            self._auto_cleanup()
            key = self._get_key(email, purpose, tenant_id)
            
            if key not in self._cache:
                return False
            
            otp_data = self._cache[key]
            
            # Check if expired
            if otp_data['expires_at'] < datetime.utcnow():
                del self._cache[key]
                return False
            
            # Verify OTP code
            if otp_data['otp_code'] != otp_code:
                return False
            
            # Remove OTP after successful verification
            del self._cache[key]
            return True
    
    def verify_otp_any_purpose(
        self, email: str, otp_code: str, tenant_id: Optional[str] = None
    ) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """
        Verify OTP for any purpose (login or register).
        
        Args:
            email: User email
            otp_code: OTP code to verify
        
        Returns:
            tuple: (is_valid, purpose, user_data) where purpose is 'login' or 'register' or None,
                   and user_data is stored user data (for registration) or None
        """
        with self._lock:
            self._auto_cleanup()
            
            # Tenant-scoped keys (current behaviour for new OTPs)
            if tenant_id:
                for purpose in ["login", "register", "password_reset"]:
                    key = self._get_key(email, purpose, tenant_id)
                    if key not in self._cache:
                        continue
                    otp_data = self._cache[key]
                    if otp_data["expires_at"] < datetime.utcnow():
                        del self._cache[key]
                        continue
                    if otp_data["otp_code"] == otp_code:
                        user_data = otp_data.get("user_data")
                        del self._cache[key]
                        return True, purpose, user_data
                return False, None, None

            # Legacy: no tenant in token — try old keys only
            for purpose in ["login", "register", "password_reset"]:
                key = self._get_key(email, purpose, None)
                if key not in self._cache:
                    continue
                otp_data = self._cache[key]
                if otp_data["expires_at"] < datetime.utcnow():
                    del self._cache[key]
                    continue
                if otp_data["otp_code"] == otp_code:
                    user_data = otp_data.get("user_data")
                    del self._cache[key]
                    return True, purpose, user_data
            return False, None, None
    
    def remove_otp(
        self, email: str, purpose: str, tenant_id: Optional[str] = None
    ) -> None:
        """
        Remove OTP from cache.
        
        Args:
            email: User email
            purpose: Purpose of OTP ('login' or 'register')
        """
        with self._lock:
            key = self._get_key(email, purpose, tenant_id)
            if key in self._cache:
                del self._cache[key]
    
    def clear_all(self) -> None:
        """Clear all OTPs from cache."""
        with self._lock:
            self._cache.clear()


# Global OTP cache instance
otp_cache = OTPCache()
