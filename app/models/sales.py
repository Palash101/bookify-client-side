"""
Sales row shape matches PostgreSQL public.sales (Bookify):

  id, tenant_id, user_id, amount, created_at, updated_at, wallet_transaction_id,
  item_type, item_id, payment_source, created_by_type, created_by_id, transaction_id (bigint)

Stripe / gateway session ids (e.g. cs_…) live in extra_metadata.gateway_transaction_id.
Extension fields (currency, gateway, status, expires_at, sessions, package_pricing_id)
live in JSONB column extra_metadata — add if missing:

  ALTER TABLE sales ADD COLUMN IF NOT EXISTS extra_metadata JSONB DEFAULT '{}'::jsonb;
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, String, DateTime, Numeric, ForeignKey, Integer, cast
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql import func

from app.core.db.session import Base
import uuid


def _parse_dt(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class Sale(Base):
    """
    Package / wallet sale. Physical columns follow DB; app fields use extra_metadata + hybrids.
    """

    __tablename__ = "sales"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
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

    # wallet_add | package_gateway | package_wallet
    type = Column(
        "payment_source",
        String(50),
        nullable=False,
        server_default="package_gateway",
        index=True,
    )

    created_by_type = Column(String(50), nullable=True)
    created_by_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Optional numeric provider reference (DB type bigint). Not for Stripe session strings.
    provider_numeric_transaction_id = Column("transaction_id", BigInteger, nullable=True, index=True)

    extra_metadata = Column(JSONB, nullable=True)

    # ----- JSON-backed fields (same Python API as before) -----

    @hybrid_property
    def currency(self) -> str:
        v = (self.extra_metadata or {}).get("currency")
        return str(v) if v is not None else "QAR"

    @currency.setter
    def currency(self, value: Optional[str]) -> None:
        meta = dict(self.extra_metadata or {})
        if value is None:
            meta.pop("currency", None)
        else:
            meta["currency"] = str(value)
        self.extra_metadata = meta
        flag_modified(self, "extra_metadata")

    @currency.expression
    def currency(cls):  # type: ignore[no-redef]
        return func.coalesce(cls.extra_metadata["currency"].astext, "QAR")

    @hybrid_property
    def gateway(self) -> str:
        v = (self.extra_metadata or {}).get("gateway")
        return str(v) if v is not None else ""

    @gateway.setter
    def gateway(self, value: Optional[str]) -> None:
        meta = dict(self.extra_metadata or {})
        if value is None:
            meta.pop("gateway", None)
        else:
            meta["gateway"] = str(value)
        self.extra_metadata = meta
        flag_modified(self, "extra_metadata")

    @gateway.expression
    def gateway(cls):  # type: ignore[no-redef]
        return func.coalesce(cls.extra_metadata["gateway"].astext, "")

    @hybrid_property
    def status(self) -> str:
        v = (self.extra_metadata or {}).get("status")
        return str(v) if v is not None else "pending"

    @status.setter
    def status(self, value: Optional[str]) -> None:
        meta = dict(self.extra_metadata or {})
        if value is None:
            meta.pop("status", None)
        else:
            meta["status"] = str(value)
        self.extra_metadata = meta
        flag_modified(self, "extra_metadata")

    @status.expression
    def status(cls):  # type: ignore[no-redef]
        return func.coalesce(cls.extra_metadata["status"].astext, "pending")

    @hybrid_property
    def expires_at(self) -> Optional[datetime]:
        return _parse_dt((self.extra_metadata or {}).get("expires_at"))

    @expires_at.setter
    def expires_at(self, value: Optional[datetime]) -> None:
        meta = dict(self.extra_metadata or {})
        if value is None:
            meta.pop("expires_at", None)
        else:
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            meta["expires_at"] = dt.isoformat()
        self.extra_metadata = meta
        flag_modified(self, "extra_metadata")

    @expires_at.expression
    def expires_at(cls):  # type: ignore[no-redef]
        return cast(cls.extra_metadata["expires_at"].astext, DateTime(timezone=True))

    @hybrid_property
    def session_count(self) -> Optional[int]:
        raw = (self.extra_metadata or {}).get("session_count")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @session_count.setter
    def session_count(self, value: Optional[int]) -> None:
        meta = dict(self.extra_metadata or {})
        if value is None:
            meta.pop("session_count", None)
        else:
            meta["session_count"] = int(value)
        self.extra_metadata = meta
        flag_modified(self, "extra_metadata")

    @session_count.expression
    def session_count(cls):  # type: ignore[no-redef]
        return cast(cls.extra_metadata["session_count"].astext, Integer)

    @hybrid_property
    def session_type(self) -> Optional[str]:
        v = (self.extra_metadata or {}).get("session_type")
        return str(v) if v is not None else None

    @session_type.setter
    def session_type(self, value: Optional[str]) -> None:
        meta = dict(self.extra_metadata or {})
        if value is None:
            meta.pop("session_type", None)
        else:
            meta["session_type"] = str(value)
        self.extra_metadata = meta
        flag_modified(self, "extra_metadata")

    @session_type.expression
    def session_type(cls):  # type: ignore[no-redef]
        return cls.extra_metadata["session_type"].astext

    @hybrid_property
    def person_count(self) -> Optional[int]:
        raw = (self.extra_metadata or {}).get("persons")
        if raw is None:
            raw = (self.extra_metadata or {}).get("person_count")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @person_count.setter
    def person_count(self, value: Optional[int]) -> None:
        meta = dict(self.extra_metadata or {})
        if value is None:
            meta.pop("persons", None)
            meta.pop("person_count", None)
        else:
            meta["persons"] = int(value)
        self.extra_metadata = meta
        flag_modified(self, "extra_metadata")

    @person_count.expression
    def person_count(cls):  # type: ignore[no-redef]
        return cast(cls.extra_metadata["persons"].astext, Integer)

    @hybrid_property
    def gateway_transaction_id(self) -> Optional[str]:
        v = (self.extra_metadata or {}).get("gateway_transaction_id")
        return str(v) if v is not None and str(v) != "" else None

    @gateway_transaction_id.setter
    def gateway_transaction_id(self, value: Optional[str]) -> None:
        meta = dict(self.extra_metadata or {})
        if value is None or str(value) == "":
            meta.pop("gateway_transaction_id", None)
        else:
            meta["gateway_transaction_id"] = str(value)
        self.extra_metadata = meta
        flag_modified(self, "extra_metadata")

    @gateway_transaction_id.expression
    def gateway_transaction_id(cls):  # type: ignore[no-redef]
        return cls.extra_metadata["gateway_transaction_id"].astext

    @property
    def pricing_id(self) -> Optional[uuid.UUID]:
        if not self.extra_metadata:
            return None
        raw = self.extra_metadata.get("package_pricing_id")
        if raw is None:
            return None
        try:
            return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
        except (ValueError, TypeError):
            return None


# Linked wallet row context (no JSON on wallet_transactions table)
SALE_WALLET_TXN_KEY = "wallet_txn"


def backfill_sale_checkout_metadata(sale: Optional[Sale], session_id: Optional[str]) -> None:
    """
    Some rows lack gateway/currency in extra_metadata. Stripe Checkout session ids start with ``cs_``.
    Call before mutating status on success redirect/callback so JSON and hybrid fields stay aligned.
    """
    if sale is None:
        return
    sid = (session_id or "").strip()
    if sid.startswith("cs_") and not (sale.extra_metadata or {}).get("gateway"):
        sale.gateway = "stripe"
    if not (sale.extra_metadata or {}).get("currency"):
        sale.currency = "QAR"


def merge_sale_wallet_txn_meta(sale: Sale, **patch: Any) -> None:
    """Merge keys into sale.extra_metadata[SALE_WALLET_TXN_KEY] (transaction_type, status, tenant_id, …)."""
    meta = dict(sale.extra_metadata or {})
    w = dict(meta.get(SALE_WALLET_TXN_KEY) or {})
    for k, v in patch.items():
        if v is None or (isinstance(v, str) and v == ""):
            w.pop(k, None)
        else:
            w[k] = v
    meta[SALE_WALLET_TXN_KEY] = w
    sale.extra_metadata = meta
    flag_modified(sale, "extra_metadata")
