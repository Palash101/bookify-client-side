from app.core.db.session import Base

# Import all models here so Alembic can detect them
# Import in order to avoid circular dependency
from app.models.tenant import Tenant
from app.models.role import Role
from app.models.user import User
from app.models.otp import OTP
from app.models.tenant_api_key import TenantAPIKey

__all__ = ["Base", "Tenant", "User", "Role", "OTP", "TenantAPIKey"]
