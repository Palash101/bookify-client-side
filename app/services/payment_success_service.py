from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.sales import Sale, find_sale_by_gateway_session, is_package_sale, sale_status_value
from app.models.sales_transactions import (
    SalesTransactionStatus,
    SalesTransactions,
)
from app.models.user import User
from app.models.user_package import UserPackage
from app.models.wallet_transactions import WalletTransaction
from app.payments.return_urls import attach_checkout_platform_debug
from app.services.sale_expiry import apply_package_expiry_to_sale
from app.services.user_package_service import ensure_user_package_for_completed_package_sale
from app.services.packages_service.packages_service import PackagesService
from app.services.locations_service.locations_service import LocationsService
from app.services.gym_config_service import GymConfigService
from app.services.wallet_notification_service import wallet_topup_email_pending


class PaymentSuccessService:
    @staticmethod
    def handle(db: Session, session_id: str) -> dict[str, str]:
        """
        Handle gateway success redirect for Stripe Checkout session ids (cs_...).
        Returns a small debug dict for the HTTP handler to expose.
        """
        debug: dict[str, str] = {"session_id": session_id}

        # 1) Sale lookup by gateway session id
        sale = find_sale_by_gateway_session(db, session_id)
        debug["sale_found_by_session"] = "1" if sale else "0"

        # Prefer platform from initiation row (set when checkout started).
        init_txn = (
            db.query(SalesTransactions)
            .filter(SalesTransactions.gateway_txn_id == session_id)
            .order_by(SalesTransactions.created_at.desc())
            .first()
        )
        if init_txn is not None:
            attach_checkout_platform_debug(debug, init_txn.extra_metadata)

        # 2) If Sale not present, reconstruct it from initiation SalesTransactions (package)
        if sale is None and session_id.startswith("cs_"):
            init_pkg = (
                db.query(SalesTransactions)
                .filter(
                    SalesTransactions.source == "package",
                    SalesTransactions.gateway_txn_id == session_id,
                    SalesTransactions.extra_metadata["event"].astext == "created",
                )
                .order_by(SalesTransactions.created_at.desc())
                .first()
            )
            debug["init_pkg_found"] = "1" if init_pkg else "0"
            if init_pkg and init_pkg.user_id and isinstance(init_pkg.extra_metadata, dict):
                meta = init_pkg.extra_metadata or {}
                attach_checkout_platform_debug(debug, meta)
                client_order_id = meta.get("client_order_id")
                if client_order_id:
                    order_uuid = UUID(str(client_order_id))
                    pkg_raw = meta.get("package_id")
                    pricing_raw = meta.get("package_pricing_id")
                    if pkg_raw and PackagesService.is_one_time_duplicate_purchase(
                        db,
                        tenant_id=init_pkg.tenant_id,
                        user_id=init_pkg.user_id,
                        package_id=UUID(str(pkg_raw)),
                    ):
                        debug["error"] = "package_already_purchased"
                        return debug
                    sale = Sale(
                        id=order_uuid,
                        tenant_id=init_pkg.tenant_id,
                        user_id=init_pkg.user_id,
                        package_id=UUID(str(pkg_raw)) if pkg_raw else None,
                        product_item_type="package",
                        type="gateway",
                        wallet_transaction_id=None,
                        amount=init_pkg.amount or 0,
                    )
                    db.add(sale)
                    db.flush()
                    init_pkg.sales_id = sale.id
                    debug["sale_created_from_init_pkg"] = "1"

        # 3) If still no Sale, reconstruct it from initiation SalesTransactions (wallet top-up)
        if sale is None:
            init_wallet = (
                db.query(SalesTransactions)
                .filter(
                    SalesTransactions.source == "wallet",
                    SalesTransactions.gateway_txn_id == session_id,
                    SalesTransactions.extra_metadata["event"].astext == "created",
                )
                .order_by(SalesTransactions.created_at.desc())
                .first()
            )
            debug["init_wallet_found"] = "1" if init_wallet else "0"
            if init_wallet and init_wallet.user_id:
                attach_checkout_platform_debug(debug, init_wallet.extra_metadata)
                user = db.query(User).filter(User.id == init_wallet.user_id).first()
                before = float(user.wallet or 0) if user else 0.0
                credited = float(init_wallet.amount or 0)
                after = before + credited

                default_currency = GymConfigService.get_currency(db, init_wallet.tenant_id)
                wtxn = WalletTransaction(
                    user_id=init_wallet.user_id,
                    direction="credit",
                    transaction_id=session_id,
                    amount=init_wallet.amount or 0,
                    currency=(init_wallet.currency or default_currency).upper(),
                    balance_before=before,
                    balance_after=after,
                    created_by=init_wallet.created_by_type,
                    created_by_id=init_wallet.created_by_id,
                    reference_type="wallet_add",
                )
                db.add(wtxn)
                db.flush()

                sale = Sale(
                    tenant_id=init_wallet.tenant_id,
                    user_id=init_wallet.user_id,
                    package_id=wtxn.id,
                    product_item_type="wallet",
                    type="gateway",
                    wallet_transaction_id=wtxn.id,
                    amount=init_wallet.amount or 0,
                )
                db.add(sale)
                db.flush()

                wtxn.reference_id = sale.id
                init_wallet.sales_id = sale.id
                init_wallet.status = SalesTransactionStatus.success
                m = dict(init_wallet.extra_metadata or {})
                m.setdefault("event", "created")
                m["resolved_by"] = "success_redirect"
                init_wallet.extra_metadata = m
                sale.provider_numeric_transaction_id = init_wallet.id
                if user:
                    user.wallet = after
                debug["wallet_topup_wallet_transaction_id"] = str(wtxn.id)
                debug["sale_created_from_init_wallet"] = "1"

        if sale is None:
            debug["error"] = "missing_initiation_sales_transaction"
            return debug

        # 4) Reconcile package entitlement + sales_transactions status
        st = (sale_status_value(db, sale) or "").lower()
        if st not in ("succeeded", "success"):
            st = "succeeded"

        if is_package_sale(sale) and sale.package_id is not None:
            st_row = (
                db.query(SalesTransactions)
                .filter(
                    SalesTransactions.source == "package",
                    SalesTransactions.gateway_txn_id == session_id,
                    SalesTransactions.tenant_id == sale.tenant_id,
                )
                .order_by(SalesTransactions.created_at.desc())
                .first()
            )

            created_by_type = getattr(init_txn, "created_by_type", None) or "member"
            created_by_id = getattr(init_txn, "created_by_id", None) or sale.user_id
            default_currency = GymConfigService.get_currency(db, sale.tenant_id)

            if st_row is None:
                st_row = SalesTransactions(
                    sales_id=sale.id,
                    tenant_id=sale.tenant_id,
                    location_id=(
                        getattr(init_txn, "location_id", None)
                        or LocationsService.resolve_location_id(db, sale.tenant_id)
                    ),
                    payment_method="gateway",
                    gateway="stripe",
                    gateway_txn_id=session_id,
                    source="package",
                    status=SalesTransactionStatus.success,
                    amount=sale.amount,
                    currency=default_currency,
                    user_id=sale.user_id,
                    created_by_type=created_by_type,
                    created_by_id=created_by_id,
                    extra_metadata={"event": "success_redirect"},
                )
                db.add(st_row)
                db.flush()
            else:
                st_row.sales_id = sale.id
                if st_row.status != SalesTransactionStatus.success:
                    st_row.status = SalesTransactionStatus.success
                if not st_row.gateway:
                    st_row.gateway = "stripe"
                if not st_row.payment_method:
                    st_row.payment_method = "gateway"
                if st_row.amount is None:
                    st_row.amount = sale.amount
                if not st_row.currency:
                    st_row.currency = default_currency
                if st_row.user_id is None:
                    st_row.user_id = sale.user_id
                if not st_row.created_by_type:
                    st_row.created_by_type = created_by_type
                if st_row.created_by_id is None:
                    st_row.created_by_id = created_by_id
                if st_row.location_id is None:
                    st_row.location_id = LocationsService.resolve_location_id(
                        db, sale.tenant_id
                    )

                m = dict(st_row.extra_metadata or {})
                m.setdefault("event", "created")
                m["resolved_by"] = "success_redirect"
                m["last_event"] = "success_redirect"
                st_row.extra_metadata = m
                db.flush()
                created_by_type = st_row.created_by_type or created_by_type
                created_by_id = st_row.created_by_id or created_by_id

            sale.provider_numeric_transaction_id = st_row.id

            if PackagesService.is_one_time_duplicate_purchase(
                db,
                tenant_id=sale.tenant_id,
                user_id=sale.user_id,
                package_id=sale.package_id,
                exclude_sale_id=sale.id,
            ):
                debug["error"] = "package_already_purchased"
                return debug
            existing_user_package = (
                db.query(UserPackage).filter(UserPackage.sale_id == sale.id).first()
            )
            user_package = ensure_user_package_for_completed_package_sale(
                db,
                sale,
                created_by=created_by_type,
                created_by_id=created_by_id,
                snapshot=st_row.extra_metadata if isinstance(st_row.extra_metadata, dict) else None,
            )
            apply_package_expiry_to_sale(db, sale, sale.tenant_id, overwrite=False)
            if user_package and existing_user_package is None:
                debug["package_purchased_user_package_id"] = str(user_package.id)

        if wallet_topup_email_pending(sale):
            debug["wallet_topup_wallet_transaction_id"] = str(sale.wallet_transaction_id)

        debug["sale_id"] = str(sale.id)
        debug["sale_status"] = sale_status_value(db, sale)
        return debug

