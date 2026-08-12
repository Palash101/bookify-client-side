"""Handle browser cancel redirect for gateway checkouts (Stripe cs_... session ids)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.sales import Sale
from app.models.sales_transactions import (
    SalesTransactionStatus,
    SalesTransactions,
    TERMINAL_SALES_TRANSACTION_STATUSES,
)
from app.payments.return_urls import attach_checkout_platform_debug

_TERMINAL_SALE_STATUSES = frozenset({"cancelled", "failed", "success", "succeeded"})


class PaymentCancelService:
    @staticmethod
    def handle(db: Session, session_id: str) -> dict[str, str]:
        """
        Mark initiation row as cancelled when the user abandons checkout.
        Returns ``payment_failed_sales_transaction_id`` only on the first cancel transition.
        """
        debug: dict[str, str] = {"session_id": session_id}

        init_txn = (
            db.query(SalesTransactions)
            .filter(SalesTransactions.gateway_txn_id == session_id)
            .order_by(SalesTransactions.created_at.desc())
            .first()
        )
        if init_txn is None:
            sale = (
                db.query(Sale)
                .filter(Sale.gateway_transaction_id == session_id)
                .first()
            )
            if sale is None:
                debug["error"] = "missing_initiation_sales_transaction"
                return debug

            attach_checkout_platform_debug(debug, sale.extra_metadata)
            previous = (sale.status or "").lower()
            debug["order_id"] = str(sale.id)
            if previous not in _TERMINAL_SALE_STATUSES:
                sale.status = "cancelled"
                if sale.provider_numeric_transaction_id is not None:
                    debug["payment_failed_sales_transaction_id"] = str(
                        sale.provider_numeric_transaction_id
                    )
                else:
                    st = (
                        db.query(SalesTransactions)
                        .filter(SalesTransactions.sales_id == sale.id)
                        .order_by(SalesTransactions.created_at.desc())
                        .first()
                    )
                    if st is not None:
                        debug["payment_failed_sales_transaction_id"] = str(st.id)
            return debug

        previous = init_txn.status
        meta = dict(init_txn.extra_metadata or {})
        attach_checkout_platform_debug(debug, meta)
        sales_id = init_txn.sales_id or meta.get("client_order_id")
        if sales_id:
            debug["order_id"] = str(sales_id)

        if previous not in TERMINAL_SALES_TRANSACTION_STATUSES:
            init_txn.status = SalesTransactionStatus.cancelled
            meta.setdefault("event", "created")
            meta["resolved_by"] = "cancel_redirect"
            meta["last_event"] = "cancel_redirect"
            init_txn.extra_metadata = meta
            debug["payment_failed_sales_transaction_id"] = str(init_txn.id)

        return debug
