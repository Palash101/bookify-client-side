"""Handle browser cancel redirect for gateway checkouts (Stripe cs_... session ids)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.sales import Sale
from app.models.sales_transactions import SalesTransactions

_TERMINAL_TXN_STATUSES = frozenset({"cancelled", "failed", "success", "succeeded"})


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

            previous = (sale.status or "").lower()
            debug["order_id"] = str(sale.id)
            debug["event_tenant_id"] = str(sale.tenant_id)
            if previous not in _TERMINAL_TXN_STATUSES:
                sale.status = "cancelled"
                if sale.provider_numeric_transaction_id is not None:
                    debug["payment_failed_sales_transaction_id"] = str(
                        sale.provider_numeric_transaction_id
                    )
                else:
                    st = (
                        db.query(SalesTransactions)
                        .filter(SalesTransactions.order_id == sale.id)
                        .order_by(SalesTransactions.created_at.desc())
                        .first()
                    )
                    if st is not None:
                        debug["payment_failed_sales_transaction_id"] = str(st.id)
            return debug

        previous = (init_txn.status or "").lower()
        meta = dict(init_txn.extra_metadata or {})
        order_id = init_txn.order_id or meta.get("client_order_id")
        debug["event_tenant_id"] = str(init_txn.tenant_id)
        if order_id:
            debug["order_id"] = str(order_id)

        if previous not in _TERMINAL_TXN_STATUSES:
            init_txn.status = "cancelled"
            meta.setdefault("event", "created")
            meta["resolved_by"] = "cancel_redirect"
            meta["last_event"] = "cancel_redirect"
            init_txn.extra_metadata = meta
            debug["payment_failed_sales_transaction_id"] = str(init_txn.id)

        return debug
