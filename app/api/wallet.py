from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.dependencies import get_current_active_user, get_current_tenant_id
from app.core.db.session import get_db
from app.models.user import User
from app.models.wallet_transactions import WalletTransaction
from app.models.sales import Sale
from app.models.sales_transactions import SalesTransactions
from app.payments.base import PaymentRequest
from app.payments.factory import get_gateway


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

    txn = WalletTransaction(
        user_id=current_user.id,
        order_id=None,
        direction="credit",
        transaction_type="wallet_add",
        transaction_id=None,
        status="pending",
        metadata_={
            "purpose": "wallet_add",
            "tenant_id": str(tenant_id),
        },
        amount=body.amount,
        currency=str(currency_code).upper(),
        balance_before=balance_before,
        balance_after=None,
        created_by=current_user.user_type or "member",
        created_by_id=current_user.id,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)

    # Create a Sales row too (wallet top-up should appear in sales)
    sale = Sale(
        tenant_id=tenant_id,
        user_id=current_user.id,
        package_id=None,
        pricing_id=None,
        type="wallet_add",
        wallet_transaction_id=txn.id,
        amount=body.amount,
        currency=str(currency_code).upper(),
        gateway=gateway.GATEWAY_TYPE.value,
        status="pending",
        extra_metadata={
            "purpose": "wallet_add",
        },
    )
    db.add(sale)
    db.commit()
    db.refresh(sale)

    # Link wallet transaction to this sales record for callback correlation
    txn.order_id = str(sale.id)
    db.commit()

    sale_txn = SalesTransactions(
        order_id=sale.id,
        tenant_id=tenant_id,
        type="wallet_add",
        gateway=gateway.GATEWAY_TYPE.value,
        gateway_txn_id=None,
        event_type="created",
        status="pending",
        amount=body.amount,
        currency=str(currency_code).upper(),
        raw_payload=None,
    )
    db.add(sale_txn)
    db.commit()

    payment_request = PaymentRequest(
        amount=body.amount,
        currency=str(currency_code).upper(),
        order_id=str(sale.id),
        customer_email=current_user.email or "",
        customer_name=f"{current_user.first_name or ''} {current_user.last_name or ''}".strip()
        or "Customer",
        description="Wallet top-up",
        metadata={
            "wallet_transaction_id": str(txn.id),
            "user_id": str(current_user.id),
            "purpose": "wallet_add",
        },
    )

    response = gateway.create_payment(payment_request)
    if not response.success:
        txn.status = "failed"
        sale.status = "failed"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=response.error_message or "Payment initiation failed.",
        )

    txn.transaction_id = response.transaction_id
    sale.gateway_transaction_id = response.transaction_id
    sale_txn.gateway_txn_id = response.transaction_id or ""
    db.commit()

    return {
        "success": True,
        "message": "Wallet top-up initiated",
        "data": {
            "wallet_transaction_id": str(txn.id),
            "order_id": str(sale.id),
            "payment_url": response.payment_url,
            "transaction_id": response.transaction_id,
            "gateway": response.gateway,
            "status": response.status,
        },
    }

