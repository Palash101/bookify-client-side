"""
FastAPI Payment Routes.

All payment operations go through these endpoints.
The active gateway is resolved per-tenant via the factory.
"""

from typing import Any, Optional, Literal
from uuid import UUID
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from .base import GatewayType, PaymentRequest
from .factory import get_gateway, TenantPaymentSettings
from app.dependencies import get_current_tenant_id, get_current_active_user, get_db
from app.models.user import User
from app.models.sales import Sale
from app.models.package_pricing import PackagePricing
from app.models.package import Package
from app.models.sales_transactions import SalesTransactions
from app.models.wallet_transactions import WalletTransaction

# Use a single, consistent tag name for Swagger ("payments")
router = APIRouter(prefix="/payment", tags=["payments"])


# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------

class InitiatePaymentRequest(BaseModel):
    order_id: str
    amount: float = Field(..., gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    customer_email: EmailStr
    customer_name: str
    description: str = ""
    gateway_override: Optional[str] = Field(
        default=None,
        description="Force a specific gateway (e.g. 'stripe'). Uses tenant default if omitted.",
    )
    metadata: dict[str, Any] = {}


class VerifyPaymentRequest(BaseModel):
    transaction_id: str
    gateway: Optional[str] = None


class RefundRequest(BaseModel):
    transaction_id: str
    amount: float = Field(..., gt=0)
    reason: str = ""
    gateway: Optional[str] = None


class PackagePurchaseRequest(BaseModel):
    """
    Request body for package purchase flow.
    Frontend se:
      - package_id
      - package_pricing_id (selected price / session option)
      - persons (kitne log is purchase me cover honge)
      - optional payment_gateway
    aayega. Amount/currency backend pricing se derive hota hai.
    """
    package_id: UUID
    package_pricing_id: UUID
    persons: Optional[int] = Field(
        default=1,
        description="Number of persons for this purchase (e.g. 2 for partner training)",
        gt=0,
    )
    payment_method: Literal["gateway", "wallet"] = Field(
        default="gateway",
        description="Use 'gateway' for online payment or 'wallet' for wallet deduction.",
    )
    payment_gateway: Optional[str] = Field(
        default=None,
        description="Which gateway to use (e.g. 'stripe', 'paypal'). If omitted, tenant default is used.",
    )


class PackagePurchaseWalletRequest(BaseModel):
    """Request body for package purchase paid from wallet (no gateway)."""
    package_id: UUID
    package_pricing_id: UUID
    persons: Optional[int] = Field(default=1, gt=0)


# ---------------------------------------------------------------------------
# Dependency: extract tenant_id using existing TenantMiddleware + dependency
# ---------------------------------------------------------------------------

def get_tenant_id(tenant_id: UUID = Depends(get_current_tenant_id)) -> str:
    """
    Resolve tenant from X-Tenant-Key header (via TenantMiddleware)
    and return it as string for the payment factory.
    """
    return str(tenant_id)


@router.post("/package-purchase")
async def initiate_package_purchase(
    body: PackagePurchaseRequest,
    tenant_id: str = Depends(get_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    High-level API for package purchase.

    - Creates a Sale row for the current user + package.
    - Resolves tenant's active payment gateway.
    - Creates a payment session and returns hosted payment URL.
    """
    # Derive amount/currency from selected package pricing
    pricing_query = (
        db.query(PackagePricing, Package)
        .join(Package, Package.id == PackagePricing.package_id)
        .filter(
            PackagePricing.id == body.package_pricing_id,
            PackagePricing.package_id == body.package_id,
        )
    )
    pricing_row = pricing_query.first()

    if pricing_row:
        pricing, package = pricing_row
    else:
        pricing, package = None, None

    if not pricing or pricing.price is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pricing not configured for this package",
        )

    # Validate persons against pricing rule (if defined)
    persons_requested = body.persons or 1
    if pricing.persons is not None and persons_requested != pricing.persons:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This pricing is valid for exactly {pricing.persons} person(s).",
        )

    amount_value = float(pricing.price)
    currency_code = "QAR"  # TODO: if multi-currency later, derive from tenant/settings

    # --------------------------
    # WALLET payment method
    # --------------------------
    if body.payment_method == "wallet":
        balance_before = float(current_user.wallet or 0)
        if balance_before < amount_value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient wallet balance. Required: {amount_value} {currency_code}, available: {balance_before}",
            )

        balance_after = balance_before - amount_value

        wallet_txn = WalletTransaction(
            user_id=current_user.id,
            order_id=None,  # set after sale created
            direction="debit",
            transaction_type="package_wallet_purchase",
            transaction_id=None,
            status="succeeded",
            metadata_={
                "purpose": "package_purchase",
                "tenant_id": tenant_id,
                "package_id": str(body.package_id),
                "package_pricing_id": str(pricing.id),
            },
            amount=amount_value,
            currency=currency_code,
            balance_before=balance_before,
            balance_after=balance_after,
            created_by=current_user.user_type or "member",
            created_by_id=current_user.id,
        )
        db.add(wallet_txn)
        db.flush()

        current_user.wallet = balance_after

        order = Sale(
            tenant_id=UUID(tenant_id),
            user_id=current_user.id,
            package_id=body.package_id,
            pricing_id=pricing.id,
            type="package_wallet",
            wallet_transaction_id=wallet_txn.id,
            amount=amount_value,
            currency=currency_code,
            gateway="wallet",
            gateway_transaction_id=None,
            status="succeeded",
            extra_metadata={
                "persons": persons_requested,
                "session_type": pricing.session_type,
                "session_count": pricing.session_count,
            },
        )
        db.add(order)
        db.flush()

        wallet_txn.order_id = str(order.id)

        tz = timezone.utc
        if package:
            if package.validity_days is not None:
                order.expires_at = datetime.now(tz) + timedelta(days=package.validity_days)
            elif package.validity_end is not None:
                order.expires_at = datetime.combine(
                    package.validity_end,
                    datetime.max.time(),
                    tzinfo=tz,
                )

        sale_txn = SalesTransactions(
            order_id=order.id,
            tenant_id=order.tenant_id,
            type="package_wallet",
            gateway="wallet",
            gateway_txn_id=None,
            event_type="created",
            status="succeeded",
            amount=amount_value,
            currency=currency_code,
            raw_payload=None,
        )
        db.add(sale_txn)
        db.commit()
        db.refresh(order)

        return {
            "order_id": str(order.id),
            "gateway": "wallet",
            "status": "succeeded",
            "amount": amount_value,
            "currency": currency_code,
        }

    # --------------------------
    # GATEWAY payment method
    # --------------------------
    gateway = get_gateway(tenant_id, body.payment_gateway)

    # Create sale in our DB (type=package_gateway = payment via gateway)
    order = Sale(
        tenant_id=UUID(tenant_id),
        user_id=current_user.id,
        package_id=body.package_id,
        pricing_id=pricing.id,
        type="package_gateway",
        wallet_transaction_id=None,
        amount=amount_value,
        currency=currency_code,
        gateway=gateway.GATEWAY_TYPE.value,
        status="pending",
        extra_metadata={
            "persons": persons_requested,
            "session_type": pricing.session_type,
            "session_count": pricing.session_count,
        },
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    # Log initial transaction event (sale created / payment initiated)
    txn = SalesTransactions(
        order_id=order.id,
        tenant_id=order.tenant_id,
        type="package_gateway",
        gateway=gateway.GATEWAY_TYPE.value,
        gateway_txn_id=None,
        event_type="created",
        status="pending",
        amount=amount_value,
        currency=currency_code,
        raw_payload=None,
    )
    db.add(txn)
    db.commit()

    # Initiate payment with gateway
    payment_request = PaymentRequest(
        amount=amount_value,
        currency=currency_code,
        order_id=str(order.id),
        customer_email=current_user.email or "",
        customer_name=f"{current_user.first_name or ''} {current_user.last_name or ''}".strip()
        or "Customer",
        description=package.name if package and package.name else f"Package purchase {body.package_id}",
        metadata={
            "package_id": str(body.package_id),
            "package_pricing_id": str(pricing.id),
            "persons": persons_requested,
            "session_type": pricing.session_type,
            "session_count": pricing.session_count,
        },
    )

    response = gateway.create_payment(payment_request)

    if not response.success:
        # Mark order as failed to initiate
        order.status = "failed"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=response.error_message or "Payment initiation failed.",
        )

    # Store gateway transaction ID on the order
    order.gateway_transaction_id = response.transaction_id

    # Also update the "created" transaction with gateway_txn_id, or insert new record
    txn.gateway_txn_id = response.transaction_id or ""
    db.commit()

    return {
        "order_id":        str(order.id),
        "payment_url":     response.payment_url,
        "transaction_id":  response.transaction_id,
        "gateway":         response.gateway,
        "status":          response.status,
    }


@router.post("/package-purchase-wallet")
async def package_purchase_with_wallet(
    body: PackagePurchaseWalletRequest,
    tenant_id: str = Depends(get_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Backward-compatible wrapper. Prefer POST /payment/package-purchase with payment_method='wallet'.
    """
    return await initiate_package_purchase(
        body=PackagePurchaseRequest(
            package_id=body.package_id,
            package_pricing_id=body.package_pricing_id,
            persons=body.persons,
            payment_method="wallet",
        ),
        tenant_id=tenant_id,
        current_user=current_user,
        db=db,
    )


@router.get("/callback/{gateway_type}")
@router.post("/callback/{gateway_type}")
async def payment_callback(
    gateway_type: str,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    db: Session = Depends(get_db),
):
    """
    Unified callback endpoint for all gateways.
    URL pattern: /payment/callback/{gateway_type}
    Handles both GET (redirect) and POST (webhook) callbacks.
    """
    # Merge query params + JSON body into one payload dict
    payload: dict[str, Any] = dict(request.query_params)

    try:
        json_body = await request.json()
        if isinstance(json_body, dict):
            payload.update(json_body)
    except Exception:
        pass  # Not a JSON request — that's fine

    # For Stripe webhooks we need the raw body for signature verification
    if gateway_type == GatewayType.STRIPE.value:
        payload["raw_body"] = await request.body()
        payload["stripe_signature"] = request.headers.get("stripe-signature", "")

    gateway = get_gateway(tenant_id, gateway_type)
    result = gateway.handle_callback(payload)

    def _wallet_status_from_gateway(status_value: Any) -> str:
        s = status_value.value if hasattr(status_value, "value") else str(status_value)
        s = s.lower()
        if s in ("success", "succeeded"):
            return "succeeded"
        if s in ("failed",):
            return "failed"
        if s in ("cancelled", "canceled"):
            return "cancelled"
        if s in ("refunded",):
            return "reversed"
        return s

    # ----------------------------------------------------------------
    # Persist result to orders + transaction log
    # ----------------------------------------------------------------
    if result.order_id:
        try:
            order_uuid = UUID(result.order_id)
        except ValueError:
            order_uuid = None

        if order_uuid is not None:
            order = db.query(Sale).filter(
                Sale.id == order_uuid,
                Sale.tenant_id == UUID(tenant_id),
            ).first()

            if order:
                # Update order status + gateway txn id
                raw_status = result.status.value if hasattr(result.status, "value") else str(result.status)
                # For wallet_add, keep status aligned with wallet mapping (succeeded/failed/cancelled)
                order.status = (
                    _wallet_status_from_gateway(result.status)
                    if getattr(order, "type", None) == "wallet_add"
                    else raw_status
                )
                order.gateway_transaction_id = result.transaction_id or order.gateway_transaction_id

                # Calculate expiry based on package validity (if available)
                package = (
                    db.query(Package)
                    .filter(Package.id == order.package_id, Package.tenant_id == UUID(tenant_id))
                    .first()
                )
                if package:
                    expires_at: Optional[datetime] = None

                    # 1) If validity_days set, use created_at + days
                    if package.validity_days is not None and order.created_at is not None:
                        expires_at = order.created_at + timedelta(days=package.validity_days)
                    # 2) Else, if validity_end date set, use that day's end
                    elif package.validity_end is not None:
                        expires_at = datetime.combine(
                            package.validity_end,
                            datetime.max.time(),
                            tzinfo=order.created_at.tzinfo if order.created_at else None,
                        )

                    if expires_at is not None:
                        order.expires_at = expires_at

            txn = SalesTransactions(
                order_id=order.id if order else order_uuid,
                tenant_id=UUID(tenant_id),
                type=order.type if order and getattr(order, "type", None) else "package_gateway",
                gateway=result.gateway.value if hasattr(result.gateway, "value") else str(result.gateway),
                gateway_txn_id=result.transaction_id or "",
                event_type="callback",
                status=result.status.value if hasattr(result.status, "value") else str(result.status),
                amount=result.amount,
                currency=result.currency,
                raw_payload=result.raw_payload,
            )
            db.add(txn)
            db.commit()

    # ----------------------------------------------------------------
    # Persist result to wallet transactions (top-ups)
    # ----------------------------------------------------------------
    if result.transaction_id:
        wallet_txn = (
            db.query(WalletTransaction)
            .filter(WalletTransaction.transaction_id == result.transaction_id)
            .first()
        )
    else:
        wallet_txn = None

    if wallet_txn is None and result.order_id:
        wallet_txn = (
            db.query(WalletTransaction)
            .filter(WalletTransaction.order_id == result.order_id)
            .order_by(WalletTransaction.created_at.desc())
            .first()
        )

    if wallet_txn:
        new_status = _wallet_status_from_gateway(result.status)

        # Idempotency: don't double-credit
        if wallet_txn.status != "succeeded":
            wallet_txn.status = new_status

            if (
                new_status == "succeeded"
                and wallet_txn.direction == "credit"
                and wallet_txn.transaction_type == "wallet_add"
            ):
                user = db.query(User).filter(User.id == wallet_txn.user_id).first()
                if user:
                    before = float(user.wallet or 0)
                    credited = float(wallet_txn.amount or 0)
                    after = before + credited
                    user.wallet = after
                    wallet_txn.balance_before = before
                    wallet_txn.balance_after = after

            db.commit()

    return {
        "success":        result.success,
        "order_id":       result.order_id,
        "transaction_id": result.transaction_id,
        "status":         result.status,
        "gateway":        result.gateway,
        "amount":         result.amount,
        "currency":       result.currency,
    }


@router.get("/gateways")
async def list_active_gateways(tenant_id: str = Depends(get_tenant_id)):
    """
    Return which payment gateways are configured for the current tenant.
    Useful for apps that want to show multiple payment options
    (e.g. Stripe / PayPal) based on tenant setup.
    """
    settings = TenantPaymentSettings.get(tenant_id)
    configured = list(settings.get("gateways", {}).keys())
    active = settings.get("active_gateway")

    return {
        "active_gateway":      active,
        "configured_gateways": configured,
    }