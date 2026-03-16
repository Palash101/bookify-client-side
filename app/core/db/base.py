from app.core.db.session import Base

# Import all models here so Alembic can detect them
# Import in order to avoid circular dependency
from app.models.tenant import Tenant
from app.models.role import Role
from app.models.user import User
from app.models.otp import OTP
from app.models.tenant_api_key import TenantAPIKey
from app.models.class_schedule import ClassSchedule
from app.models.gym_class import GymClass
from app.models.package_discount import PackageDiscount
from app.models.package import Package
from app.models.package_pricing import PackagePricing
from app.models.fitness_program import FitnessProgram
from app.models.tenant_payment_settings import TenantPaymentSettings
from app.models.package_order import PackageOrder
from app.models.package_purchase_transaction import PackagePurchaseTransaction

__all__ = [
    "Base",
    "Tenant",
    "User",
    "Role",
    "OTP",
    "TenantAPIKey",
    "ClassSchedule",
    "GymClass",
    "PackageDiscount",
    "Package",
    "PackagePricing",
    "FitnessProgram",
    "TenantPaymentSettings",
    "PackageOrder",
    "PackagePurchaseTransaction",
]
