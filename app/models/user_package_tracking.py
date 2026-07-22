import enum
from typing import Any

from sqlalchemy import BigInteger, Column, DateTime, Enum, ForeignKey, Integer, Sequence, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.core.db.session import Base


class SessionTxnType(str, enum.Enum):
    credit = "credit"
    debit = "debit"


class SessionTxnSource(str, enum.Enum):
    booking = "booking"
    purchase = "purchase"
    refund = "refund"
    admin = "admin"


def session_txn_type_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, SessionTxnType):
        return value.value
    return str(value).strip().lower()


def normalize_session_txn_type(value: Any) -> SessionTxnType:
    if isinstance(value, SessionTxnType):
        return value
    raw = value.value if hasattr(value, "value") else str(value)
    mapping = {
        "credit": SessionTxnType.credit,
        "debit": SessionTxnType.debit,
    }
    return mapping.get(raw.strip().lower(), SessionTxnType.debit)


def session_txn_source_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, SessionTxnSource):
        return value.value
    return str(value).strip().lower()


def normalize_session_txn_source(value: Any) -> SessionTxnSource:
    if isinstance(value, SessionTxnSource):
        return value
    raw = value.value if hasattr(value, "value") else str(value)
    mapping = {
        "booking": SessionTxnSource.booking,
        "purchase": SessionTxnSource.purchase,
        "refund": SessionTxnSource.refund,
        "admin": SessionTxnSource.admin,
    }
    return mapping.get(raw.strip().lower(), SessionTxnSource.admin)


class UserPackageTracking(Base):
    """
    Session ledger for user package entitlements.
    Each row records a credit/debit against user_packages.session_count.
    Mirrors public.user_package_tracking in PostgreSQL.
    """

    __tablename__ = "user_package_tracking"

    id = Column(
        BigInteger,
        Sequence("user_package_session_transaction_id_seq"),
        primary_key=True,
    )

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_package_id = Column(
        UUID(as_uuid=True),
        ForeignKey("user_packages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    txn_type = Column(
        "type",
        Enum(SessionTxnType, name="session_txn_type", create_type=False),
        nullable=False,
        index=True,
    )
    txn_source = Column(
        "source",
        Enum(SessionTxnSource, name="session_txn_source", create_type=False),
        nullable=False,
        index=True,
    )

    sessions = Column(Integer, nullable=False)
    balance_after = Column(Integer, nullable=True)

    reference_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
