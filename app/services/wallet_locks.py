"""Row locks for money changes (wallet balance, payment processing).

Rows are always locked in one order -- class -> booking -> member -> package -- here and in
the staff dashboard (fitnez_main_backend booking / sales services), so concurrent
bookings, cancels, top-ups and payment callbacks queue instead of overwriting a balance,
refunding twice or crediting a top-up twice. Locks are held until the transaction commits.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sales_transactions import SalesTransactions
from app.models.user import User


def lock_member(db: Session, user_id) -> Optional[User]:
    """The member's row, locked, with its committed values (re-read even if already loaded)."""
    return db.query(User).filter(User.id == user_id).with_for_update().populate_existing().first()


def lock_payment_rows(db: Session, gateway_txn_id: Optional[str]) -> None:
    """Lock a payment's initiation rows, so one handler at a time processes it."""
    if gateway_txn_id:
        db.execute(
            select(SalesTransactions.id)
            .where(SalesTransactions.gateway_txn_id == str(gateway_txn_id))
            .with_for_update()
        )
