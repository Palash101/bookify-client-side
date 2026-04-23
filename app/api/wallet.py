from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import String

from app.dependencies import get_current_active_user, get_current_tenant_id
from app.core.db.session import get_db
from app.models.user import User
from app.models.wallet_transactions import WalletTransaction
from app.schemas.transactions import (
    WalletTransactionsListResponse,
    WalletBalanceResponse,
    PurchasesHistoryResponse,
    PurchaseHistoryItemResponse,
    PurchasesHistoryDataResponse,
)
from app.models.sales import SALE_WALLET_TXN_KEY, Sale, merge_sale_wallet_txn_meta
from app.models.sales_transactions import SalesTransactions
from app.payments.base import PaymentRequest
from app.payments.factory import get_gateway
from app.models.package import Package


router = APIRouter(tags=["wallet"])


class AddWalletBalanceRequest(BaseModel):
    amount: float = Field(..., gt=0)
    payment_gateway: Optional[str] = Field(
        default=None,
        description="Which gateway to use (e.g. 'stripe', 'paypal'). If omitted, tenant default is used.",
    )


@router.post("/add/wallet/balance")
async def add_wallet_balance(
    body: AddWalletBalanceRequest,
    tenant_id=Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Initiate a wallet top-up payment through the tenant's configured gateway.
    Wallet balance is credited on gateway callback success.
    """
    gateway = get_gateway(str(tenant_id), body.payment_gateway)

    currency_code = (
        (gateway.settings or {}).get("currency")
        or (gateway.settings or {}).get("default_currency")
        or "QAR"
    )

    balance_before = float(current_user.wallet or 0)

    # Wallet top-up via gateway: at initiation time we only log sales_transactions.
    # Sale + wallet_transactions will be created on callback/success redirect.
    client_order_id = uuid.uuid4()
    init_txn = SalesTransactions(
        order_id=None,
        tenant_id=tenant_id,
        payment_method="gateway",
        gateway=gateway.GATEWAY_TYPE.value,
        gateway_txn_id=None,
        source="wallet",
        status="pending",
        amount=body.amount,
        currency=str(currency_code).upper(),
        user_id=current_user.id,
        created_by_type=current_user.user_type or "member",
        created_by_id=current_user.id,
        extra_metadata={
            "event": "created",
            "client_order_id": str(client_order_id),
            "purpose": "wallet_add",
            "direction": "credit",
            "balance_before": str(balance_before),
        },
    )
    db.add(init_txn)
    db.commit()

    payment_request = PaymentRequest(
        amount=body.amount,
        currency=str(currency_code).upper(),
        order_id=str(client_order_id),
        customer_email=current_user.email or "",
        customer_name=f"{current_user.first_name or ''} {current_user.last_name or ''}".strip()
        or "Customer",
        description="Wallet top-up",
        metadata={
            "user_id": str(current_user.id),
            "purpose": "wallet_add",
        },
    )

    response = gateway.create_payment(payment_request)
    if not response.success:
        init_txn.status = "failed"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=response.error_message or "Payment initiation failed.",
        )

    init_txn.gateway_txn_id = response.transaction_id or ""
    db.commit()

    return {
        "success": True,
        "message": "Wallet top-up initiated",
        "data": {
            "order_id": str(client_order_id),
            "payment_url": response.payment_url,
            "transaction_id": response.transaction_id,
            "gateway": response.gateway,
            "status": response.status,
        },
    }


@router.get("/balance", response_model=WalletBalanceResponse)
async def get_wallet_balance(
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    # Tenant header ka mismatch ho sakta hai (token kis tenant ka hai us par depend karta hai).
    # Wallet data `current_user.id` se scoped hoga, isliye strict tenant mismatch error na do.
    _ = tenant_id

    # Currency multi-tenant config not yet wired here; return last txn currency if available
    last_txn = (
        db.query(WalletTransaction)
        .filter(WalletTransaction.user_id == current_user.id)
        .order_by(WalletTransaction.created_at.desc())
        .first()
    )
    currency = (getattr(last_txn, "currency", None) or "QAR")

    return {
        "success": True,
        "message": "Wallet balance fetched successfully",
        "data": {
            "wallet": float(current_user.wallet or 0),
            "currency": str(currency),
        },
    }


@router.get("/transactions", response_model=WalletTransactionsListResponse)
async def get_wallet_transactions(
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
):
    # Transactions are scoped to current_user via user_id; tenant header mismatch shouldn't block.
    _ = tenant_id

    txns = (
        db.query(WalletTransaction)
        .filter(WalletTransaction.user_id == current_user.id)
        .order_by(WalletTransaction.created_at.desc())
        .limit(limit)
        .all()
    )

    return {
        "success": True,
        "message": "Wallet transactions fetched successfully",
        "data": [
            {
                "id": str(t.id),
                "user_id": t.user_id,
                "order_id": t.order_id,
                "direction": t.direction,
                "transaction_type": t.transaction_type,
                "transaction_id": t.transaction_id,
                "status": t.status,
                "metadata": t.metadata_,
                "amount": t.amount,
                "currency": t.currency,
                "balance_before": t.balance_before,
                "balance_after": t.balance_after,
                "created_by": t.created_by,
                "created_by_id": t.created_by_id,
                "created_at": t.created_at,
            }
            for t in txns
        ],
        "count": len(txns),
    }


@router.get(
    "/transactions/purchases",
    response_model=PurchasesHistoryResponse,
)
async def get_purchases_history(
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
):
    """
    User ne kya kya purchase kiya (wallet add, package gateway, package wallet).
    sales.type isko differentiate karta hai:
      - wallet_add
      - package_gateway
      - package_wallet (legacy) / wallet (new)
    """
    # Scope by token user tenant (security). Header mismatch shouldn't block.
    scoped_tenant_id = current_user.tenant_id

    sales = (
        db.query(Sale, Package.name.label("package_name"))
        .outerjoin(Package, Package.id == Sale.package_id)
        .filter(
            Sale.user_id == current_user.id,
            Sale.tenant_id == scoped_tenant_id,
        )
        .order_by(Sale.created_at.desc())
        .limit(limit)
        .all()
    )

    data = PurchasesHistoryDataResponse()
    for sale, package_name in sales:
        purchased_at = sale.created_at

        # Map payment_method from sale.type
        if sale.type in ("package_wallet", "wallet") and (sale.product_item_type or "") == "package":
            payment_method = "wallet"
        else:
            payment_method = "gateway"

        item = PurchaseHistoryItemResponse(
            sale_id=sale.id,
            type=sale.type,
            purchased_at=purchased_at,
            status=sale.status,
            amount=sale.amount,
            currency=sale.currency,
            payment_method=payment_method,
            gateway=sale.gateway,
            gateway_transaction_id=sale.gateway_transaction_id,
            package_id=sale.package_id,
            package_name=package_name,
            pricing_id=sale.pricing_id,
            wallet_transaction_id=sale.wallet_transaction_id,
        )

        if (sale.product_item_type or "") == "wallet":
            data.wallet_adds.append(item)
        elif sale.type in ("package_gateway", "gateway") and (sale.product_item_type or "") == "package":
            data.package_gateway_purchases.append(item)
        elif sale.type in ("package_wallet", "wallet") and (sale.product_item_type or "") == "package":
            data.package_wallet_purchases.append(item)

    return PurchasesHistoryResponse(data=data)

