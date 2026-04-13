"""
FastAPI Payment Routes.

All payment operations go through these endpoints.
The active gateway is resolved per-tenant via the factory.
"""

from typing import Any, Optional, Literal
from uuid import UUID
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
import logging
from uuid import uuid4

from .base import GatewayType, PaymentRequest
from .factory import get_gateway, TenantPaymentSettings
from app.dependencies import get_current_tenant_id, get_current_active_user, get_db
from app.core.settings import settings
from app.models.user import User
from app.models.sales import SALE_WALLET_TXN_KEY, Sale, backfill_sale_checkout_metadata
from app.models.package_pricing import PackagePricing
from app.models.package import Package
from app.models.sales_transactions import SalesTransactions
from app.models.wallet_transactions import WalletTransaction
from app.models.tenant_payment_settings import TenantPaymentSettings as TenantPaymentSettingsModel
from app.services.sale_expiry import apply_package_expiry_to_sale
from app.services.user_package_service import ensure_user_package_for_completed_package_sale
from app.schemas.transactions import SalesTransactionsListResponse

# Use a single, consistent tag name for Swagger ("payments")
router = APIRouter(prefix="/payment", tags=["payments"])
logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Dependency: extract tenant_id using existing TenantMiddleware + dependency
# ---------------------------------------------------------------------------

def get_tenant_id(tenant_id: UUID = Depends(get_current_tenant_id)) -> str:
    """
    Resolve tenant from X-Tenant-Key header (via TenantMiddleware)
    and return it as string for the payment factory.
    """
    return str(tenant_id)

def _resolve_tenant_for_stripe_webhook(db: Session, raw_body: bytes, stripe_signature: str) -> Optional[str]:
    """
    Stripe webhooks do not include X-Tenant-Key. We resolve tenant by trying to
    verify the signature against each tenant's configured webhook_secret.
    Returns tenant_id as string if matched, else None.
    """
    try:
        import stripe as stripe_lib  # type: ignore
    except Exception:
        return None

    rows = (
        db.query(TenantPaymentSettingsModel)
        .filter(TenantPaymentSettingsModel.gateway_type == GatewayType.STRIPE.value)
        .all()
    )

    for row in rows:
        settings = row.payment_config or {}
        secret = settings.get("webhook_secret")
        if not secret:
            continue
        try:
            stripe_lib.Webhook.construct_event(raw_body, stripe_signature, secret)
            return str(row.tenant_id)
        except Exception:
            continue

    return None


@router.post("/package-purchase")
async def initiate_package_purchase(
    body: PackagePurchaseRequest,
    tenant_id: str = Depends(get_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Package purchase.

    - **Gateway package:** `sales` + `sales_transactions` (+ `user_packages` when payment succeeds).
    - **Wallet balance package:** `wallet_transactions` (debit) + `sales` + `sales_transactions` + `user_packages`.
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
            direction="debit",
            transaction_id=None,
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
            product_item_type="package",
            type="package_wallet",
            created_by_type=current_user.user_type or "member",
            created_by_id=current_user.id,
            wallet_transaction_id=wallet_txn.id,
            amount=amount_value,
            extra_metadata={
                "persons": persons_requested,
                "session_type": pricing.session_type,
                "session_count": pricing.session_count,
                "package_pricing_id": str(pricing.id),
                "currency": currency_code,
                "gateway": "wallet",
                "status": "succeeded",
                SALE_WALLET_TXN_KEY: {
                    "transaction_type": "package_wallet_purchase",
                    "status": "succeeded",
                    "purpose": "package_purchase",
                    "tenant_id": tenant_id,
                    "package_id": str(body.package_id),
                    "package_pricing_id": str(pricing.id),
                },
            },
        )
        db.add(order)
        db.flush()

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
            payment_method="cash",
            gateway="wallet",
            gateway_txn_id=None,
            source="package",
            status="success",
            amount=amount_value,
            currency=currency_code,
            user_id=current_user.id,
            created_by_type=current_user.user_type or "member",
            created_by_id=current_user.id,
            extra_metadata={"event": "created"},
        )
        db.add(sale_txn)
        db.flush()
        order.provider_numeric_transaction_id = sale_txn.id
        ensure_user_package_for_completed_package_sale(
            db,
            order,
            created_by=current_user.user_type or "member",
            created_by_id=current_user.id,
        )
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
    try:
        gateway = get_gateway(tenant_id, body.payment_gateway)
    except (ValueError, KeyError, ImportError) as exc:
        # Most common reasons: tenant has no gateway config, invalid gateway type,
        # or provider SDK is missing in the deployed image.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # Create a client-side order UUID for the gateway callback correlation.
    # We will create the Sale + UserPackage only after gateway reports success.
    client_order_id = uuid4()

    # Log initial transaction event (payment initiated; no Sale yet)
    txn = SalesTransactions(
        order_id=None,
        tenant_id=UUID(tenant_id),
        payment_method="gateway",
        gateway=gateway.GATEWAY_TYPE.value,
        gateway_txn_id=None,
        source="package",
        status="pending",
        amount=amount_value,
        currency=currency_code,
        user_id=current_user.id,
        created_by_type=current_user.user_type or "member",
        created_by_id=current_user.id,
        extra_metadata={
            "event": "created",
            "client_order_id": str(client_order_id),
            "package_id": str(body.package_id),
            "package_pricing_id": str(pricing.id),
            "persons": persons_requested,
            "session_type": pricing.session_type,
            "session_count": pricing.session_count,
        },
    )
    db.add(txn)
    db.commit()

    # Initiate payment with gateway
    payment_request = PaymentRequest(
        amount=amount_value,
        currency=currency_code,
        order_id=str(client_order_id),
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

    try:
        response = gateway.create_payment(payment_request)
    except Exception as exc:
        error_id = str(uuid4())
        logger.exception(
            "package-purchase gateway.create_payment crashed (error_id=%s, tenant_id=%s, gateway=%s, order_id=%s)",
            error_id,
            tenant_id,
            getattr(gateway, "GATEWAY_TYPE", None),
            str(client_order_id),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Payment gateway error (error_id={error_id}). "
                + (f"{type(exc).__name__}: {exc}" if settings.DEBUG else "Please try again later.")
            ),
        ) from exc

    if not response.success:
        # Mark initiation log as failed
        txn.status = "failed"
        txn.gateway_txn_id = response.transaction_id or txn.gateway_txn_id or ""
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=response.error_message or "Payment initiation failed.",
        )

    # Update the "created" transaction with gateway_txn_id
    txn.gateway_txn_id = response.transaction_id or ""
    db.commit()

    return {
        "order_id":        str(client_order_id),
        "payment_url":     response.payment_url,
        "transaction_id":  response.transaction_id,
        "gateway":         response.gateway,
        "status":          response.status,
    }


@router.get("/callback/{gateway_type}")
@router.post("/callback/{gateway_type}")
async def payment_callback(
    gateway_type: str,
    request: Request,
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

    tenant_id: Optional[str] = None

    # For Stripe webhooks we need the raw body for signature verification and tenant auto-resolve
    if gateway_type == GatewayType.STRIPE.value:
        payload["raw_body"] = await request.body()
        payload["stripe_signature"] = request.headers.get("stripe-signature", "")
        tenant_id = _resolve_tenant_for_stripe_webhook(
            db,
            payload["raw_body"],
            payload.get("stripe_signature", ""),
        )
        if tenant_id is None:
            raise HTTPException(status_code=401, detail="Unable to resolve tenant for stripe webhook")
    else:
        # For non-stripe gateways, keep requiring tenant header/middleware
        tenant_id = None
        try:
            tenant_uuid = await get_current_tenant_id(request)
            tenant_id = str(tenant_uuid)
        except Exception:
            raise HTTPException(status_code=401, detail="X-Tenant-Key header is required")

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

            # When we only log initiation in sales_transactions, the Sale is created here on success.
            if order is None:
                init_txn = (
                    db.query(SalesTransactions)
                    .filter(
                        SalesTransactions.tenant_id == UUID(tenant_id),
                        SalesTransactions.source == "package",
                        SalesTransactions.extra_metadata["client_order_id"].astext == str(order_uuid),
                        SalesTransactions.extra_metadata["event"].astext == "created",
                    )
                    .order_by(SalesTransactions.created_at.desc())
                    .first()
                )
                if init_txn and init_txn.user_id:
                    meta = init_txn.extra_metadata or {}
                    pkg_raw = meta.get("package_id")
                    pricing_raw = meta.get("package_pricing_id")
                    order = Sale(
                        id=order_uuid,
                        tenant_id=UUID(tenant_id),
                        user_id=init_txn.user_id,
                        package_id=UUID(str(pkg_raw)) if pkg_raw else None,
                        product_item_type="package",
                        type="package_gateway",
                        created_by_type=init_txn.created_by_type,
                        created_by_id=init_txn.created_by_id,
                        wallet_transaction_id=None,
                        amount=init_txn.amount or 0,
                        extra_metadata={
                            "persons": meta.get("persons"),
                            "session_type": meta.get("session_type"),
                            "session_count": meta.get("session_count"),
                            "package_pricing_id": str(pricing_raw) if pricing_raw else None,
                            "currency": init_txn.currency or (result.currency or "QAR"),
                            "gateway": (
                                result.gateway.value
                                if hasattr(result.gateway, "value")
                                else str(result.gateway)
                            ),
                            "status": _wallet_status_from_gateway(result.status),
                            "gateway_transaction_id": result.transaction_id,
                        },
                    )
                    db.add(order)
                    db.flush()
                    init_txn.order_id = order.id

            audit_sale = order or db.query(Sale).filter(Sale.id == order_uuid).first()
            if audit_sale:
                backfill_sale_checkout_metadata(audit_sale, result.transaction_id)

            if order:
                # Update order status + gateway txn id
                # Normalize statuses so app can rely on one spelling.
                # Stripe returns "success" but app expects "succeeded".
                order.status = _wallet_status_from_gateway(result.status)
                order.gateway_transaction_id = result.transaction_id or order.gateway_transaction_id

                if order.package_id is not None:
                    apply_package_expiry_to_sale(
                        db, order, UUID(tenant_id), overwrite=True
                    )

                if (order.status or "").lower() in ("succeeded", "success"):
                    ensure_user_package_for_completed_package_sale(
                        db,
                        order,
                        created_by=order.created_by_type or "member",
                        created_by_id=order.created_by_id or order.user_id,
                    )

            txn = SalesTransactions(
                order_id=order.id if order else order_uuid,
                tenant_id=UUID(tenant_id),
                payment_method="gateway",
                gateway=(
                    (result.gateway.value if hasattr(result.gateway, "value") else str(result.gateway))
                    or (order.gateway if order else (audit_sale.gateway if audit_sale else ""))
                ),
                gateway_txn_id=result.transaction_id or "",
                source="package",
                status=("success" if _wallet_status_from_gateway(result.status) == "succeeded" else "failed" if _wallet_status_from_gateway(result.status) in ("failed","cancelled","reversed") else _wallet_status_from_gateway(result.status)),
                amount=result.amount,
                currency=result.currency,
                user_id=order.user_id if order else (audit_sale.user_id if audit_sale else None),
                created_by_type=(
                    (audit_sale.created_by_type or "member") if audit_sale else "gateway"
                ),
                created_by_id=(
                    (audit_sale.created_by_id or audit_sale.user_id) if audit_sale else None
                ),
                extra_metadata={"event": "callback"},
            )
            db.add(txn)
            db.flush()
            if order is not None:
                order.provider_numeric_transaction_id = txn.id
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
        try:
            sale_for_wallet = db.query(Sale).filter(Sale.id == UUID(str(result.order_id))).first()
        except ValueError:
            sale_for_wallet = None
        if sale_for_wallet and sale_for_wallet.wallet_transaction_id:
            wallet_txn = (
                db.query(WalletTransaction)
                .filter(WalletTransaction.id == sale_for_wallet.wallet_transaction_id)
                .first()
            )

    if wallet_txn:
        new_status = _wallet_status_from_gateway(result.status)

        # Idempotency: don't double-credit
        if wallet_txn.status != "succeeded":
            wallet_txn.status = new_status
            ls = wallet_txn.linked_sale
            if ls:
                ls.status = new_status

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


@router.get("/sales-transactions", response_model=SalesTransactionsListResponse)
async def get_sales_transactions(
    limit: int = Query(20, ge=1, le=100),
    tenant_id: UUID = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    include_wallet_add: bool = Query(False, description="Include wallet top-ups in results"),
):
    """
    Current user's sales transaction history (package gateway/wallet payments).
    """
    type_filter = ["package_gateway", "package_wallet"]
    if include_wallet_add:
        type_filter.append("wallet_add")

    # Sale is source of truth; package_wallet rows may have no sales_transactions (by design).
    sales = (
        db.query(Sale)
        .filter(
            Sale.user_id == current_user.id,
            Sale.tenant_id == tenant_id,
            Sale.type.in_(type_filter),
        )
        .order_by(Sale.created_at.desc())
        .limit(limit)
        .all()
    )
    sale_ids = [s.id for s in sales]
    latest_st_by_order: dict = {}
    if sale_ids:
        st_rows = (
            db.query(SalesTransactions)
            .filter(SalesTransactions.order_id.in_(sale_ids))
            .order_by(SalesTransactions.created_at.desc())
            .all()
        )
        for st in st_rows:
            if st.order_id not in latest_st_by_order:
                latest_st_by_order[st.order_id] = st

    def _row(sale: Sale) -> dict[str, Any]:
        st = latest_st_by_order.get(sale.id)
        return {
            "id": st.id if st else sale.id,
            "order_id": sale.id,
            "type": sale.type,
            "payment_method": "wallet" if sale.type == "package_wallet" else "gateway",
            "purchase_source": (
                "wallet_topup"
                if sale.type == "wallet_add"
                else ("wallet_purchase" if sale.type == "package_wallet" else "gateway_purchase")
            ),
            "is_package_purchase": sale.type in ("package_gateway", "package_wallet"),
            "gateway": st.gateway if st else sale.gateway,
            "gateway_txn_id": st.gateway_txn_id if st else sale.gateway_transaction_id,
            "status": st.status if st else sale.status,
            "amount": st.amount if st is not None and st.amount is not None else sale.amount,
            "currency": st.currency if st is not None and st.currency is not None else sale.currency,
            "package_id": sale.package_id,
            "pricing_id": sale.pricing_id,
            "wallet_transaction_id": sale.wallet_transaction_id,
            "created_at": st.created_at if st else sale.created_at,
        }

    return {
        "success": True,
        "message": "Sales transactions fetched successfully",
        "data": [_row(sale) for sale in sales],
        "count": len(sales),
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