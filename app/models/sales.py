"""
Sales row shape matches PostgreSQL public.sales:

  id, tenant_id, user_id, amount, created_at, updated_at, wallet_transaction_id,
  item_type, item_id, payment_source, transaction_id (bigint)

Status / currency / gateway live on sales_transactions.
Session quota and expiry live on user_packages.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID as UUIDType

from sqlalchemy import BigInteger, Column, String, DateTime, Numeric, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session
from sqlalchemy.sql import exists, func, or_

from app.core.db.session import Base
import uuid


class Sale(Base):
    """Package / wallet sale. Physical columns follow the tenant DB."""

    __tablename__ = "sales"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)

    amount = Column(Numeric(10, 2), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=True,
    )

    wallet_transaction_id = Column(
        UUID(as_uuid=True),
        ForeignKey("wallet_transactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # DB item_type / item_id (Python: product_item_type + package_id)
    product_item_type = Column("item_type", String(50), nullable=True)
    package_id = Column(
        "item_id",
        UUID(as_uuid=True),
        ForeignKey("packages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # cash | gateway | wallet | package_gateway | package_wallet | wallet_add
    type = Column(
        "payment_source",
        String(50),
        nullable=False,
        server_default="package_gateway",
        index=True,
    )

    # Optional numeric provider reference (DB type bigint). Not for Stripe session strings.
    provider_numeric_transaction_id = Column("transaction_id", BigInteger, nullable=True, index=True)


def is_package_sale(sale: Sale) -> bool:
    """True for a package purchase, including POS cash (not only gateway/wallet)."""
    if (sale.product_item_type or "").strip().lower() == "package":
        return True
    return (sale.type or "") in ("package_gateway", "package_wallet")


def package_sale_clause():
    """SQLAlchemy filter matching is_package_sale()."""
    return or_(
        Sale.product_item_type == "package",
        Sale.type.in_(["package_gateway", "package_wallet"]),
    )


def latest_sales_transaction(db: Session, sale_id: uuid.UUID):
    from app.models.sales_transactions import SalesTransactions

    return (
        db.query(SalesTransactions)
        .filter(SalesTransactions.sales_id == sale_id)
        .order_by(SalesTransactions.created_at.desc())
        .first()
    )


def sale_txn_snapshot(db: Session, sale: Sale) -> dict[str, Any]:
    txn = latest_sales_transaction(db, sale.id)
    meta = getattr(txn, "extra_metadata", None) if txn is not None else None
    return dict(meta) if isinstance(meta, dict) else {}


def sale_status_value(db: Session, sale: Sale) -> str:
    from app.models.sales_transactions import SalesTransactionStatus
    from app.models.user_package import UserPackage

    txn = latest_sales_transaction(db, sale.id)
    if txn is not None and txn.status is not None:
        raw = txn.status.value if hasattr(txn.status, "value") else str(txn.status)
        if raw == SalesTransactionStatus.success.value or raw == "success":
            return "succeeded"
        return raw
    if db.query(UserPackage.id).filter(UserPackage.sale_id == sale.id).first() is not None:
        return "succeeded"
    return "pending"


def sale_currency_value(db: Session, sale: Sale, default: str = "QAR") -> str:
    txn = latest_sales_transaction(db, sale.id)
    if txn is not None and txn.currency:
        return str(txn.currency)
    return default


def sale_gateway_value(db: Session, sale: Sale, default: str = "") -> str:
    txn = latest_sales_transaction(db, sale.id)
    if txn is not None and txn.gateway:
        return str(txn.gateway)
    return default


def sale_gateway_txn_id(db: Session, sale: Sale) -> Optional[str]:
    txn = latest_sales_transaction(db, sale.id)
    if txn is not None and txn.gateway_txn_id:
        return str(txn.gateway_txn_id)
    return None


def sale_pricing_id(db: Session, sale: Sale) -> Optional[uuid.UUID]:
    from app.models.user_package import UserPackage

    up = db.query(UserPackage).filter(UserPackage.sale_id == sale.id).first()
    if up is not None and up.pricing_id is not None:
        return up.pricing_id
    raw = sale_txn_snapshot(db, sale).get("package_pricing_id")
    if raw is None:
        return None
    try:
        return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


def sale_session_count(db: Session, sale: Sale) -> Optional[int]:
    from app.models.user_package import UserPackage

    up = db.query(UserPackage).filter(UserPackage.sale_id == sale.id).first()
    if up is not None:
        if up.total_session is not None:
            return int(up.total_session)
        if up.session_count is not None:
            return int(up.session_count)
    raw = sale_txn_snapshot(db, sale).get("session_count")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def sale_session_type(db: Session, sale: Sale) -> Optional[str]:
    from app.models.user_package import UserPackage

    up = db.query(UserPackage).filter(UserPackage.sale_id == sale.id).first()
    if up is not None and up.session_type:
        return str(up.session_type)
    raw = sale_txn_snapshot(db, sale).get("session_type")
    return str(raw) if raw else None


def sale_person_count(db: Session, sale: Sale) -> Optional[int]:
    from app.models.user_package import UserPackage

    up = db.query(UserPackage).filter(UserPackage.sale_id == sale.id).first()
    if up is not None and up.person_count is not None:
        return int(up.person_count)
    snap = sale_txn_snapshot(db, sale)
    raw = snap.get("persons")
    if raw is None:
        raw = snap.get("person_count")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def sale_expires_at(db: Session, sale: Sale):
    from app.models.user_package import UserPackage

    up = db.query(UserPackage).filter(UserPackage.sale_id == sale.id).first()
    if up is not None:
        return up.expire_at
    return None


def sale_succeeded_clause():
    """True when a success sales_transaction exists, or a user_packages entitlement row."""
    from sqlalchemy.orm import aliased

    from app.models.sales_transactions import SalesTransactionStatus, SalesTransactions
    from app.models.user_package import UserPackage

    up = aliased(UserPackage)
    return or_(
        exists().where(
            (SalesTransactions.sales_id == Sale.id)
            & (SalesTransactions.status == SalesTransactionStatus.success)
        ),
        exists().where(up.sale_id == Sale.id),
    )


def find_sale_by_gateway_session(db: Session, session_id: str) -> Optional[Sale]:
    from app.models.sales_transactions import SalesTransactions

    txn = (
        db.query(SalesTransactions)
        .filter(SalesTransactions.gateway_txn_id == session_id)
        .order_by(SalesTransactions.created_at.desc())
        .first()
    )
    if txn is None or txn.sales_id is None:
        return None
    return db.query(Sale).filter(Sale.id == txn.sales_id).first()


def parse_pricing_id(raw: Any) -> Optional[UUIDType]:
    if raw is None:
        return None
    try:
        return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None
