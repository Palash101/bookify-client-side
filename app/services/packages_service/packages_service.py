from typing import Any, Dict, List, Optional
import uuid

from fastapi import HTTPException, status
from sqlalchemy import func as sa_func_sql
from sqlalchemy.orm import Session, joinedload

from app.models.class_booking import ClassBooking
from app.models.package import Package
from app.models.package_pricing import PackagePricing
from app.models.sales import Sale
from app.models.user_package import UserPackage
from app.services.bookings_service import ACTIVE_USER_BOOKING_STATUSES, _sessions_remaining_from_sale
from app.services.sale_expiry import compute_sale_expires_at


class PackagesService:
    @staticmethod
    def list_packages(
        db: Session,
        tenant_id: uuid.UUID,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "asc",
    ) -> List[Package]:
        """
        List packages for a tenant with optional search and sorting.
        """
        query = (
            db.query(Package)
            .options(
                joinedload(Package.pricing_list).joinedload(PackagePricing.discount)
            )
            .filter(Package.tenant_id == tenant_id)
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
    def get_package_detail(db: Session, tenant_id: uuid.UUID, package_id: uuid.UUID) -> Package:
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
        tenant_id: uuid.UUID,
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
        tenant_id: uuid.UUID,
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
                if up.session_count is not None:
                    total_sessions = int(up.session_count)
                elif pricing_row is not None and pricing_row.session_count is not None:
                    total_sessions = int(pricing_row.session_count)

            sessions_remaining: Optional[int] = None
            if is_unlimited:
                sessions_remaining = None
            else:
                # Prefer sale JSON override (if present), else compute from entitlement total - used
                if sale is not None:
                    rem_meta = _sessions_remaining_from_sale(sale)
                    if rem_meta is not None:
                        sessions_remaining = max(0, int(rem_meta))
                if sessions_remaining is None and total_sessions is not None:
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

