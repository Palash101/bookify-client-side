from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.sales import Sale, SALE_WALLET_TXN_KEY, backfill_sale_checkout_metadata
from app.models.sales_transactions import SalesTransactions
from app.models.user import User
from app.models.wallet_transactions import WalletTransaction
from app.services.sale_expiry import apply_package_expiry_to_sale
from app.services.user_package_service import ensure_user_package_for_completed_package_sale


class PaymentSuccessService:
    @staticmethod
    def handle(db: Session, session_id: str) -> dict[str, str]:
        """
        Handle gateway success redirect for Stripe Checkout session ids (cs_...).
        Returns a small debug dict for the HTTP handler to expose.
        """
        debug: dict[str, str] = {"session_id": session_id}

        # 1) Sale lookup by gateway session id
        sale = db.query(Sale).filter(Sale.gateway_transaction_id == session_id).first()
        debug["sale_found_by_session"] = "1" if sale else "0"

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
                client_order_id = meta.get("client_order_id")
                if client_order_id:
                    order_uuid = UUID(str(client_order_id))
                    pkg_raw = meta.get("package_id")
                    pricing_raw = meta.get("package_pricing_id")
                    sale = Sale(
                        id=order_uuid,
                        tenant_id=init_pkg.tenant_id,
                        user_id=init_pkg.user_id,
                        package_id=UUID(str(pkg_raw)) if pkg_raw else None,
                        product_item_type="package",
                        type="package_gateway",
                        created_by_type=init_pkg.created_by_type,
                        created_by_id=init_pkg.created_by_id,
                        wallet_transaction_id=None,
                        amount=init_pkg.amount or 0,
                        extra_metadata={
                            "persons": meta.get("persons"),
                            "session_type": meta.get("session_type"),
                            "session_count": meta.get("session_count"),
                            "package_pricing_id": str(pricing_raw) if pricing_raw else None,
                            "currency": init_pkg.currency or "QAR",
                            "gateway": "stripe",
                            "status": "succeeded",
                            "gateway_transaction_id": session_id,
                        },
                    )
                    db.add(sale)
                    db.flush()
                    init_pkg.order_id = sale.id
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
                user = db.query(User).filter(User.id == init_wallet.user_id).first()
                before = float(user.wallet or 0) if user else 0.0
                credited = float(init_wallet.amount or 0)
                after = before + credited

                wtxn = WalletTransaction(
                    user_id=init_wallet.user_id,
                    direction="credit",
                    transaction_id=session_id,
                    amount=init_wallet.amount or 0,
                    currency=(init_wallet.currency or "QAR").upper(),
                    balance_before=before,
                    balance_after=after,
                    created_by=init_wallet.created_by_type,
                    created_by_id=init_wallet.created_by_id,
                )
                db.add(wtxn)
                db.flush()

                sale = Sale(
                    tenant_id=init_wallet.tenant_id,
                    user_id=init_wallet.user_id,
                    package_id=wtxn.id,
                    product_item_type="wallet",
                    type="gateway",
                    created_by_type=init_wallet.created_by_type,
                    created_by_id=init_wallet.created_by_id,
                    wallet_transaction_id=wtxn.id,
                    amount=init_wallet.amount or 0,
                    extra_metadata={
                        "purpose": "wallet_add",
                        "currency": (init_wallet.currency or "QAR").upper(),
                        "gateway": "stripe",
                        "status": "succeeded",
                        "gateway_transaction_id": session_id,
                        SALE_WALLET_TXN_KEY: {
                            "transaction_type": "wallet_add",
                            "status": "succeeded",
                            "tenant_id": str(init_wallet.tenant_id),
                        },
                    },
                )
                db.add(sale)
                db.flush()

                init_wallet.order_id = sale.id
                init_wallet.status = "success"
                m = dict(init_wallet.extra_metadata or {})
                m.setdefault("event", "created")
                m["resolved_by"] = "success_redirect"
                init_wallet.extra_metadata = m
                sale.provider_numeric_transaction_id = init_wallet.id
                if user:
                    user.wallet = after
                debug["sale_created_from_init_wallet"] = "1"

        if sale is None:
            debug["error"] = "missing_initiation_sales_transaction"
            return debug

        # 4) Reconcile Sale (metadata + package entitlement)
        backfill_sale_checkout_metadata(sale, session_id)
        st = (sale.status or "").lower()
        if st not in ("succeeded", "success"):
            sale.status = "succeeded"
            st = "succeeded"

        is_package_sale = (sale.type or "") in ("package_gateway", "package_wallet")
        if is_package_sale and sale.package_id is not None:
            apply_package_expiry_to_sale(db, sale, sale.tenant_id, overwrite=False)
            if st in ("succeeded", "success"):
                ensure_user_package_for_completed_package_sale(
                    db,
                    sale,
                    created_by=sale.created_by_type or "member",
                    created_by_id=sale.created_by_id or sale.user_id,
                )

                exists = (
                    db.query(SalesTransactions)
                    .filter(
                        SalesTransactions.order_id == sale.id,
                        SalesTransactions.source == "package",
                        SalesTransactions.extra_metadata["event"].astext == "success_redirect",
                    )
                    .first()
                )
                if exists is None:
                    st_row = SalesTransactions(
                        order_id=sale.id,
                        tenant_id=sale.tenant_id,
                        payment_method="gateway",
                        gateway=sale.gateway or "stripe",
                        gateway_txn_id=session_id,
                        source="package",
                        status="success",
                        amount=sale.amount,
                        currency=sale.currency,
                        user_id=sale.user_id,
                        created_by_type=sale.created_by_type or "member",
                        created_by_id=sale.created_by_id or sale.user_id,
                        extra_metadata={"event": "success_redirect"},
                    )
                    db.add(st_row)
                    db.flush()
                    sale.provider_numeric_transaction_id = st_row.id

        debug["sale_id"] = str(sale.id)
        debug["sale_status"] = str(sale.status)
        return debug

