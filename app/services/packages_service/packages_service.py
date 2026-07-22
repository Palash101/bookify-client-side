from typing import Any, Dict, List, Optional
from datetime import date, datetime
import uuid

from fastapi import HTTPException, status
from sqlalchemy import func as sa_func_sql, or_
from sqlalchemy.orm import Session, joinedload

from app.models.class_booking import ClassBooking
from app.models.package import Package
from app.models.package_discount import PackageDiscount
from app.models.package_pricing import PackagePricing
from app.models.sales import Sale
from app.models.user_package import UserPackage
from app.schemas.package import PackageResponse
from app.services.bookings_service import ACTIVE_USER_BOOKING_STATUSES, _sessions_remaining_from_sale
from app.services.gym_config_service import GymConfigService
from app.services.sale_expiry import compute_sale_expires_at
from app.services.user_package_tracking_service import sessions_remaining_for_sale

# Admin "published" packages use status=active (draft/block are hidden from clients).
_CATALOG_STATUS = "active"


class PackagesService:
    @staticmethod
    def compute_discounted_purchase_amount(pricing: PackagePricing) -> float:
        """
        Final charge for a package pricing row after its linked discount (if any).
        Discount types: flat/fixed (subtract amount) or percentage/percent (reduce by %).
        """
        if pricing.price is None:
            raise ValueError("Package pricing has no price configured")

        base = float(pricing.price)
        discount: Optional[PackageDiscount] = pricing.discount
        if discount is None or discount.value is None:
            return round(base, 2)

        discount_value = float(discount.value)
        discount_type = (discount.type or "").strip().lower()

        if discount_type in ("percentage", "percent", "pct"):
            final = base * (1 - discount_value / 100)
        else:
            # flat, fixed, amount, or unknown — subtract fixed value
            final = base - discount_value

        return round(max(0.0, final), 2)

    _DISCOUNT_METADATA_KEYS = (
        "discount_id",
        "discount_type",
        "discount_value",
        "original_price",
        "discount_amount",
    )

    @staticmethod
    def discount_metadata_from(meta: Optional[dict[str, Any]]) -> dict[str, Any]:
        if not meta:
            return {}
        return {k: meta[k] for k in PackagesService._DISCOUNT_METADATA_KEYS if k in meta}

    @staticmethod
    def is_one_time_package(package: Package) -> bool:
        return (package.package_type or "").strip().lower() == "one_time"

    @staticmethod
    def tenant_today(db: Session, tenant_id: str) -> date:
        gym_config = GymConfigService.get_gym_config(db, tenant_id)
        tz = GymConfigService.resolve_zoneinfo(gym_config)
        return datetime.now(tz).date()

    @staticmethod
    def is_published_package(package: Package) -> bool:
        """Published packages are stored as status=active."""
        return (package.status or "").strip().lower() == _CATALOG_STATUS

    @staticmethod
    def is_visible_by_date(package: Package, today: date) -> bool:
        """
        Catalog visibility window uses packages.validity_start and packages.validity_end.
        Show only when validity_start has arrived and validity_end has not passed.
        """
        start = package.validity_start
        if start is not None and start > today:
            return False
        end = package.validity_end
        if end is not None and end < today:
            return False
        return True

    @staticmethod
    def is_package_available_in_catalog(package: Package, today: date) -> bool:
        return (
            PackagesService.is_published_package(package)
            and PackagesService.is_visible_by_date(package, today)
        )

    @staticmethod
    def user_has_succeeded_package_purchase(
        db: Session,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        package_id: uuid.UUID,
        exclude_sale_id: Optional[uuid.UUID] = None,
    ) -> bool:
        """True if the user already has a successful package sale for this package."""
        q = db.query(Sale.id).filter(
            Sale.tenant_id == tenant_id,
            Sale.user_id == user_id,
            Sale.package_id == package_id,
            (
                Sale.type.in_(["package_gateway", "package_wallet"])
                | ((Sale.type == "gateway") & (Sale.product_item_type == "package"))
                | ((Sale.type == "wallet") & (Sale.product_item_type == "package"))
            ),
            Sale.package_id.isnot(None),
            Sale.status.in_(["succeeded", "success"]),
        )
        if exclude_sale_id is not None:
            q = q.filter(Sale.id != exclude_sale_id)
        return q.first() is not None

    @staticmethod
    def assert_user_can_purchase_package(
        db: Session,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        package: Package,
    ) -> None:
        """Raise 400 when package is not catalog-visible or one-time already purchased."""
        today = PackagesService.tenant_today(db, tenant_id)
        if not PackagesService.is_published_package(package):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This package is not available for purchase.",
            )
        if not PackagesService.is_visible_by_date(package, today):
            if (
                package.validity_end is not None
                and package.validity_end < today
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="This package is no longer available for purchase.",
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This package is not available for purchase yet.",
            )
        if not PackagesService.is_one_time_package(package):
            return
        if PackagesService.user_has_succeeded_package_purchase(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            package_id=package.id,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You have already purchased this package. It can only be bought once.",
            )

    @staticmethod
    def is_one_time_duplicate_purchase(
        db: Session,
        *,
        tenant_id: str,
        user_id: uuid.UUID,
        package_id: uuid.UUID,
        exclude_sale_id: Optional[uuid.UUID] = None,
    ) -> bool:
        """Non-HTTP check used on gateway success/callback to block duplicate one-time entitlements."""
        package = (
            db.query(Package)
            .filter(Package.id == package_id, Package.tenant_id == tenant_id)
            .first()
        )
        if package is None or not PackagesService.is_one_time_package(package):
            return False
        return PackagesService.user_has_succeeded_package_purchase(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            package_id=package_id,
            exclude_sale_id=exclude_sale_id,
        )

    @staticmethod
    def package_catalog_flags(
        db: Session,
        *,
        tenant_id: str,
        user_id: Optional[uuid.UUID],
        package: Package,
    ) -> dict[str, Optional[bool]]:
        """Purchase flags for catalog APIs when the caller is authenticated."""
        if user_id is None:
            return {"already_purchased": None, "can_purchase": None}
        already = PackagesService.user_has_succeeded_package_purchase(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            package_id=package.id,
        )
        can_purchase = not (PackagesService.is_one_time_package(package) and already)
        return {"already_purchased": already, "can_purchase": can_purchase}

    @staticmethod
    def should_show_package_in_catalog(
        db: Session,
        *,
        tenant_id: str,
        user_id: Optional[uuid.UUID],
        package: Package,
    ) -> bool:
        """
        Catalog rules:
        - only published (status=active) packages
        - validity_start must be on or before tenant today
        - validity_end must be on or after tenant today (when set)
        - hide one-time packages the user has already purchased
        """
        today = PackagesService.tenant_today(db, tenant_id)
        if not PackagesService.is_package_available_in_catalog(package, today):
            return False
        if user_id is None:
            return True
        flags = PackagesService.package_catalog_flags(
            db, tenant_id=tenant_id, user_id=user_id, package=package
        )
        if PackagesService.is_one_time_package(package) and flags["already_purchased"]:
            return False
        return True

    @staticmethod
    def package_to_response(
        db: Session,
        *,
        tenant_id: str,
        package: Package,
        user_id: Optional[uuid.UUID] = None,
    ) -> PackageResponse:
        base = PackageResponse.model_validate(package)
        flags = PackagesService.package_catalog_flags(
            db, tenant_id=tenant_id, user_id=user_id, package=package
        )
        return base.model_copy(update=flags)

    @staticmethod
    def build_purchase_discount_metadata(pricing: PackagePricing, amount_value: float) -> dict[str, Any]:
        if pricing.discount is None or pricing.discount.value is None:
            return {}
        original_price = float(pricing.price)
        return {
            "discount_id": str(pricing.discount.id),
            "discount_type": pricing.discount.type,
            "discount_value": float(pricing.discount.value),
            "original_price": original_price,
            "discount_amount": round(original_price - amount_value, 2),
        }

    @staticmethod
    def list_packages(
        db: Session,
        tenant_id: str,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
    ) -> List[Package]:
        """
        List catalog packages for a tenant with optional search and sorting.
        Only published (active) packages within the validity window.
        """
        today = PackagesService.tenant_today(db, tenant_id)
        query = (
            db.query(Package)
            .options(
                joinedload(Package.pricing_list).joinedload(PackagePricing.discount)
            )
            .filter(
                Package.tenant_id == tenant_id,
                Package.status == _CATALOG_STATUS,
                or_(
                    Package.validity_start.is_(None),
                    Package.validity_start <= today,
                ),
                or_(
                    Package.validity_end.is_(None),
                    Package.validity_end >= today,
                ),
            )
        )

        # Simple text search on name
        if search:
            like = f"%{search}%"
            query = query.filter(Package.name.ilike(like))

        # Sorting
        sort_column = None
        if sort_by == "name":
            sort_column = Package.name
        elif sort_by == "created_at":
            sort_column = Package.created_at
        elif sort_by == "validity_days":
            sort_column = Package.validity_days
        elif sort_by == "sort_order":
            sort_column = Package.sort_order

        if sort_column is not None:
            query = query.order_by(
                sort_column.asc() if sort_order.lower() == "asc" else sort_column.desc()
            )
        else:
            # Default ordering
            query = query.order_by(Package.sort_order, Package.created_at)

        return query.all()

    @staticmethod
    def get_package_detail(db: Session, tenant_id: str, package_id: uuid.UUID) -> Package:
        package = (
            db.query(Package)
            .options(
                joinedload(Package.pricing_list).joinedload(PackagePricing.discount)
            )
            .filter(Package.id == package_id, Package.tenant_id == tenant_id)
            .first()
        )
        if not package:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
        return package

    @staticmethod
    def _active_package_entry_for_order(
        db: Session,
        tenant_id: str,
        order: Sale,
    ) -> Optional[Dict[str, Any]]:
        """
        Build one active-package payload dict for a single sale (order).
        """
        package = (
            db.query(Package)
            .filter(Package.id == order.package_id, Package.tenant_id == tenant_id)
            .first()
        )
        if package is None:
            return None

        sessions_used_raw = (
            db.query(sa_func_sql.coalesce(sa_func_sql.sum(ClassBooking.sessions_deducted), 0))
            .filter(
                ClassBooking.user_package_purchase_id == order.id,
                ClassBooking.status.in_(list(ACTIVE_USER_BOOKING_STATUSES)),
            )
            .scalar()
        )
        try:
            sessions_used = int(sessions_used_raw or 0)
        except (TypeError, ValueError):
            sessions_used = 0

        meta = order.extra_metadata if isinstance(order.extra_metadata, dict) else {}
        pricing_row = None
        if order.pricing_id:
            pricing_row = (
                db.query(PackagePricing)
                .filter(PackagePricing.id == order.pricing_id)
                .first()
            )

        is_unlimited = bool(
            pricing_row.is_unlimited
            if pricing_row is not None and pricing_row.is_unlimited is not None
            else False
        )

        session_type = meta.get("session_type")
        if not session_type and pricing_row is not None:
            session_type = pricing_row.session_type

        total_raw = meta.get("session_count")
        if total_raw is None and pricing_row is not None and pricing_row.session_count is not None:
            total_raw = pricing_row.session_count
        total_sessions: Optional[int] = None
        if not is_unlimited and total_raw is not None:
            try:
                total_sessions = int(total_raw)
            except (TypeError, ValueError):
                total_sessions = None

        sessions_remaining: Optional[int] = None
        if is_unlimited:
            sessions_remaining = None
        else:
            rem_meta = _sessions_remaining_from_sale(order)
            if rem_meta is not None:
                sessions_remaining = max(0, int(rem_meta))
            elif total_sessions is not None:
                sessions_remaining = max(0, total_sessions - sessions_used)

        expires_at = order.expires_at or compute_sale_expires_at(order, package)

        return {
            "id": order.id,
            "package_id": package.id,
            "package_name": package.name,
            "package_description": package.description,
            "validity_days": package.validity_days,
            "validity_end": package.validity_end,
            "status": order.status,
            "purchased_at": order.created_at,
            "expires_at": expires_at,
            "sale_type": order.type,
            "amount": order.amount,
            "currency": order.currency,
            "session_type": session_type,
            "is_unlimited": is_unlimited,
            "session_count": total_sessions,
            "sessions_remaining": sessions_remaining,
            "sessions_used": sessions_used,
        }
        

    @staticmethod
    def get_active_packages_for_user(
        db: Session,
        tenant_id: str,
        user_id: uuid.UUID,
    ) -> List[Dict[str, Any]]:
        """
        Active packages for this user+tenant.
        Source of truth is `user_packages` (entitlements). We optionally join `sales`
        to enrich with amount/currency and to ensure only succeeded purchases are returned.
        """
        from sqlalchemy.sql import func as sa_func

        out: List[Dict[str, Any]] = []
        rows = (
            db.query(UserPackage, Sale)
            # We only want "active packages" that can actually be used for booking,
            # so require a real Sale row for the entitlement.
            .join(Sale, Sale.id == UserPackage.sale_id)
            .filter(
                UserPackage.user_id == user_id,
                UserPackage.package_id.isnot(None),
                # Expiry check comes from entitlement row
                (UserPackage.expire_at.is_(None)) | (UserPackage.expire_at > sa_func.now()),
                # Tenant scoping and payment constraints live on Sale.
                (Sale.tenant_id == tenant_id),
                Sale.status.in_(["succeeded", "success"]),
                (
                    (Sale.type.in_(["package_gateway", "package_wallet"]))
                    | ((Sale.type == "gateway") & (Sale.product_item_type == "package"))
                    | ((Sale.type == "wallet") & (Sale.product_item_type == "package"))
                ),
            )
            .order_by(UserPackage.created_at.desc())
            .all()
        )

        for up, sale in rows:
            package = (
                db.query(Package)
                .filter(Package.id == up.package_id, Package.tenant_id == tenant_id)
                .first()
            )
            if package is None:
                continue

            sessions_used_raw = (
                db.query(sa_func_sql.coalesce(sa_func_sql.sum(ClassBooking.sessions_deducted), 0))
                .filter(
                    ClassBooking.user_package_purchase_id == (up.sale_id or (sale.id if sale else None)),
                    ClassBooking.status.in_(list(ACTIVE_USER_BOOKING_STATUSES)),
                )
                .scalar()
            )
            try:
                sessions_used = int(sessions_used_raw or 0)
            except (TypeError, ValueError):
                sessions_used = 0

            pricing_row = None
            if up.pricing_id:
                pricing_row = db.query(PackagePricing).filter(PackagePricing.id == up.pricing_id).first()

            is_unlimited = bool(
                pricing_row.is_unlimited
                if pricing_row is not None and pricing_row.is_unlimited is not None
                else False
            )

            session_type = up.session_type or (pricing_row.session_type if pricing_row is not None else None)

            total_sessions: Optional[int] = None
            if not is_unlimited:
                if up.total_session is not None:
                    total_sessions = int(up.total_session)
                elif sale is not None and sale.session_count is not None:
                    total_sessions = int(sale.session_count)
                elif pricing_row is not None and pricing_row.session_count is not None:
                    total_sessions = int(pricing_row.session_count)

            sessions_remaining: Optional[int] = None
            if is_unlimited:
                sessions_remaining = None
            elif sale is not None:
                sessions_remaining = sessions_remaining_for_sale(db, sale)
            elif total_sessions is not None:
                sessions_remaining = max(0, total_sessions - sessions_used)

            expires_at = up.expire_at or (sale.expires_at if sale is not None else None) or compute_sale_expires_at(
                sale, package
            ) if sale is not None else up.expire_at

            out.append(
                {
                    # API contract: this id is the sale id to be used as `user_package_purchase_id` for booking.
                    "id": sale.id,
                    "package_id": package.id,
                    "package_name": package.name,
                    "package_description": package.description,
                    "booking_restriction": package.booking_restriction,
                    "validity_days": package.validity_days,
                    "validity_end": package.validity_end,
                    "status": (sale.status if sale is not None else "succeeded"),
                    "purchased_at": (sale.created_at if sale is not None else up.created_at),
                    "expires_at": expires_at,
                    "sale_type": (sale.type if sale is not None else "package_gateway"),
                    "amount": (sale.amount if sale is not None else None),
                    "currency": (sale.currency if sale is not None else None),
                    "session_type": session_type,
                    "is_unlimited": is_unlimited,
                    "session_count": total_sessions,
                    "sessions_remaining": sessions_remaining,
                    "sessions_used": sessions_used,
                }
            )

        return out

