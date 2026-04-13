from __future__ import annotations

from typing import Any, Optional, List, Dict, Union
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class WalletTransactionItemResponse(BaseModel):
    id: UUID
    user_id: UUID

    order_id: Optional[str] = None
    direction: str
    transaction_type: str
    transaction_id: Optional[str] = None

    status: str
    metadata: Optional[Dict[str, Any]] = None

    amount: Decimal
    currency: str
    balance_before: Optional[Decimal] = None
    balance_after: Optional[Decimal] = None

    created_by: Optional[str] = None
    created_by_id: Optional[UUID] = None

    created_at: Optional[datetime] = None


class WalletTransactionsListResponse(BaseModel):
    success: bool = True
    message: str = "Wallet transactions fetched successfully"
    data: List[WalletTransactionItemResponse] = []
    count: int = 0


class WalletBalanceResponse(BaseModel):
    success: bool = True
    message: str = "Wallet balance fetched successfully"
    data: Dict[str, Any]


class SalesTransactionItemResponse(BaseModel):
    """
    Compact timeline item for app UI.
    (No raw_payload, no tenant_id, to keep response light.)
    """

    # sales_transactions.id is bigint; wallet-only sales rows use sale id (UUID).
    id: Union[int, UUID]
    order_id: UUID

    # wallet_add | package_gateway | package_wallet
    type: str
    # wallet | gateway (wallet_add me bhi "wallet" aa sakta hai)
    payment_method: str
    # App UI ke liye display-friendly label
    purchase_source: str  # "wallet_purchase" | "gateway_purchase" | "wallet_topup"
    is_package_purchase: bool = False

    gateway: str
    gateway_txn_id: Optional[str] = None

    status: str
    amount: Optional[Decimal] = None
    currency: Optional[str] = None

    package_id: Optional[UUID] = None
    pricing_id: Optional[UUID] = None
    wallet_transaction_id: Optional[UUID] = None

    created_at: Optional[datetime] = None


class SalesTransactionsListResponse(BaseModel):
    success: bool = True
    message: str = "Sales transactions fetched successfully"
    data: List[SalesTransactionItemResponse] = []
    count: int = 0


class PurchaseHistoryItemResponse(BaseModel):
    sale_id: UUID
    type: str
    purchased_at: Optional[datetime] = None
    status: Optional[str] = None

    amount: Optional[Decimal] = None
    currency: Optional[str] = None

    payment_method: str  # "wallet" | "gateway"
    gateway: Optional[str] = None
    gateway_transaction_id: Optional[str] = None

    package_id: Optional[UUID] = None
    package_name: Optional[str] = None
    pricing_id: Optional[UUID] = None

    wallet_transaction_id: Optional[UUID] = None

    class Config:
        from_attributes = True


class PurchasesHistoryDataResponse(BaseModel):
    wallet_adds: List[PurchaseHistoryItemResponse] = []
    package_gateway_purchases: List[PurchaseHistoryItemResponse] = []
    package_wallet_purchases: List[PurchaseHistoryItemResponse] = []


class PurchasesHistoryResponse(BaseModel):
    success: bool = True
    message: str = "Purchases history fetched successfully"
    data: PurchasesHistoryDataResponse

