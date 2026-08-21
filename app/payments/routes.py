"""
FastAPI Payment Routes.

All payment operations go through these endpoints.
The active gateway is resolved per-tenant via the factory.
"""

from typing import Any, Optional, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import ProgrammingError, OperationalError
import logging
from uuid import uuid4

from .base import GatewayType, PaymentRequest
from .factory import get_gateway, TenantPaymentSettings
from .redirect_handlers import (
    build_payment_cancel_response,
    build_payment_success_response,
)
from .return_urls import checkout_origin_metadata, normalize_checkout_platform
from .tenant_resolve import resolve_tenant_for_stripe_webhook
from app.core.db.session import get_session_factory
from app.dependencies import get_current_tenant_id, get_current_active_user, get_db
from app.core.settings import settings
from app.models.user import User
from app.models.sales import (
    Sale,
    sale_currency_value,
    sale_gateway_txn_id,
    sale_gateway_value,
    sale_pricing_id,
    sale_status_value,
)
from app.models.package_pricing import PackagePricing
from app.models.package import Package
from app.models.sales_transactions import (
    SalesTransactionStatus,
    SalesTransactions,
    TERMINAL_SALES_TRANSACTION_STATUSES,
    sales_transaction_status_from_gateway,
)
from app.models.wallet_transactions import WalletTransaction
from app.models.user_package import UserPackage
from app.services.sale_expiry import apply_package_expiry_to_sale
from app.services.user_package_service import ensure_user_package_for_completed_package_sale
from app.services.packages_service.packages_service import PackagesService
from app.services.locations_service.locations_service import LocationsService
from app.services.gym_config_service import GymConfigService
from app.services.package_notification_service import PackageNotificationService
from app.services.payment_notification_service import PaymentNotificationService
from app.services.wallet_notification_service import (
    WalletNotificationService,
    mark_wallet_topup_email_sent,
    wallet_topup_email_pending,
)
from app.services.event_tenant import (
    event_tenant_id_from_sale,
    event_tenant_id_from_user_package_id,
)
from app.schemas.transactions import (
    SalesTransactionsListResponse,
    normalize_display_status,
    build_pagination,
)

# Use a single, consistent tag name for Swagger ("payments")
router = APIRouter(prefix="/payment", tags=["payments"])
# Stripe dashboard URL: /api/v1/client/callback/{gateway_type}
payment_callback_router = APIRouter(prefix="/callback", tags=["payments"])
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
        description="Number of persons for this purchase; must be at least 1 and not more than the pricing max.",
        ge=1,
    )
    payment_method: Literal["gateway", "wallet"] = Field(
        default="gateway",
        description="Use 'gateway' for online payment or 'wallet' for wallet deduction.",
    )
    payment_gateway: Optional[str] = Field(
        default=None,
        description="Which gateway to use (e.g. 'stripe', 'paypal'). If omitted, tenant default is used.",
    )
    platform: Literal["web", "app"] = Field(
        ...,
        description="Required. Client that started checkout: 'web' redirects to frontend, 'app' uses deep link.",
    )
    location_id: Optional[UUID] = Field(
        default=None,
        description="Location this purchase belongs to. If omitted and the tenant has exactly one active location, that location is used.",
    )


# ---------------------------------------------------------------------------
# Dependency: extract tenant_id using existing TenantMiddleware + dependency
# ---------------------------------------------------------------------------

def get_tenant_id(tenant_id: str = Depends(get_current_tenant_id)) -> str:
    """
    Resolve tenant from X-Tenant-Key header (via TenantMiddleware)
    and return it as string for the payment factory.
    """
    return tenant_id

@router.post("/package-purchase")
async def initiate_package_purchase(
    body: PackagePurchaseRequest,
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Package purchase.

    - **Gateway package:** `sales_transactions` at initiation; `sales` + `user_packages` on success.
    - **Wallet balance package:** `wallet_transactions` (debit) + `sales` + `user_packages` only
      (no `sales_transactions` — wallet spend is not cash revenue).
    """
    # Derive amount/currency from selected package pricing (incl. discount)
    pricing_query = (
        db.query(PackagePricing, Package)
        .options(joinedload(PackagePricing.discount))
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

    # Validate persons: can be fewer than pricing max, but not more
    persons_requested = body.persons or 1
    if pricing.persons is not None and persons_requested > pricing.persons:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {pricing.persons} person(s) allowed for this pricing. You requested {persons_requested}.",
        )

    if package:
        PackagesService.assert_user_can_purchase_package(
            db,
            tenant_id=tenant_id,
            user_id=current_user.id,
            package=package,
        )

    today = PackagesService.tenant_today(db, tenant_id)
    amount_value = PackagesService.compute_discounted_purchase_amount(pricing, today=today)
    discount_meta = PackagesService.build_purchase_discount_metadata(
        pricing, amount_value, today=today
    )
    currency_code = GymConfigService.get_currency(db, tenant_id)
    location_id = LocationsService.resolve_location_id(
        db, tenant_id, body.location_id
    )

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
            reference_type="package_wallet_purchase",
        )
        db.add(wallet_txn)
        db.flush()

        current_user.wallet = balance_after

        order = Sale(
            tenant_id=tenant_id,
            user_id=current_user.id,
            package_id=body.package_id,
            product_item_type="package",
            type="wallet",
            wallet_transaction_id=wallet_txn.id,
            amount=amount_value,
        )
        db.add(order)
        db.flush()
        wallet_txn.reference_id = order.id

        package_snapshot = {
            "persons": persons_requested,
            "session_type": pricing.session_type,
            "session_count": pricing.session_count,
            "package_pricing_id": str(pricing.id),
            **discount_meta,
        }
        wallet_sale_txn = SalesTransactions(
            sales_id=order.id,
            tenant_id=tenant_id,
            location_id=location_id,
            payment_method="wallet",
            gateway="wallet",
            gateway_txn_id=None,
            source="package",
            status=SalesTransactionStatus.success,
            amount=amount_value,
            currency=currency_code,
            user_id=current_user.id,
            created_by_type=current_user.user_type or "member",
            created_by_id=current_user.id,
            extra_metadata=package_snapshot,
        )
        db.add(wallet_sale_txn)
        db.flush()
        order.provider_numeric_transaction_id = wallet_sale_txn.id

        user_package = ensure_user_package_for_completed_package_sale(
            db,
            order,
            created_by=current_user.user_type or "member",
            created_by_id=current_user.id,
            snapshot=package_snapshot,
        )
        db.commit()
        db.refresh(order)

        await WalletNotificationService.publish_debited(
            db,
            tenant_id=tenant_id,
            wallet_transaction_id=wallet_txn.id,
        )
        if user_package is not None:
            await PackageNotificationService.publish_purchased(
                db,
                tenant_id=tenant_id,
                user_package_id=user_package.id,
            )

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
    checkout_platform = normalize_checkout_platform(body.platform)

    # Log initial transaction event (payment initiated; no Sale yet)
    txn = SalesTransactions(
        sales_id=None,
        tenant_id=tenant_id,
        location_id=location_id,
        payment_method="gateway",
        gateway=gateway.GATEWAY_TYPE.value,
        gateway_txn_id=None,
        source="package",
        status=SalesTransactionStatus.pending,
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
            "checkout_platform": checkout_platform,
            **discount_meta,
            **checkout_origin_metadata(request),
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
            "checkout_platform": checkout_platform,
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
        txn.status = SalesTransactionStatus.failed
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


@router.get("/success")
async def payment_success_redirect(session_id: Optional[str] = None):
    """Browser redirect after successful checkout (Stripe, PayPal, etc.)."""
    return await build_payment_success_response(session_id)


@router.get("/cancel")
async def payment_cancel_redirect(session_id: Optional[str] = None):
    """Browser redirect when the user cancels checkout."""
    return await build_payment_cancel_response(session_id)


@router.get("/callback/{gateway_type}")
@router.post("/callback/{gateway_type}")
@payment_callback_router.get("/{gateway_type}")
@payment_callback_router.post("/{gateway_type}")
async def payment_callback(
    gateway_type: str,
    request: Request,
):
    """
    Unified callback endpoint for all gateways.
    URL patterns:
      - /api/v1/client/callback/{gateway_type}  (tenant DB default)
      - /api/v1/client/payment/callback/{gateway_type}
    Handles both GET (redirect) and POST (webhook) callbacks.
    """
    payload: dict[str, Any] = dict(request.query_params)
    tenant_id: Optional[str] = None

    # Stripe webhooks: read raw body first (signature verification requires untouched bytes).
    if gateway_type == GatewayType.STRIPE.value:
        payload["raw_body"] = await request.body()
        payload["stripe_signature"] = request.headers.get("stripe-signature", "")
        tenant_id = resolve_tenant_for_stripe_webhook(
            payload["raw_body"],
            payload.get("stripe_signature", ""),
        )
        if tenant_id is None:
            raise HTTPException(status_code=401, detail="Unable to resolve tenant for stripe webhook")
    else:
        try:
            json_body = await request.json()
            if isinstance(json_body, dict):
                payload.update(json_body)
        except Exception:
            pass
        try:
            tenant_id = await get_current_tenant_id(request)
        except Exception:
            raise HTTPException(status_code=401, detail="X-Tenant-Key header is required")

    request.state.tenant_id = tenant_id
    db = get_session_factory(tenant_id)()
    try:
        return await _handle_payment_callback(
            gateway_type=gateway_type,
            request=request,
            db=db,
            tenant_id=tenant_id,
            payload=payload,
        )
    finally:
        db.close()


async def _handle_payment_callback(
    *,
    gateway_type: str,
    request: Request,
    db: Session,
    tenant_id: str,
    payload: dict[str, Any],
) -> Any:
    gateway = get_gateway(tenant_id, gateway_type)
    result = gateway.handle_callback(payload)
    default_currency = GymConfigService.get_currency(db, tenant_id)

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
    package_purchased_user_package_id = None
    payment_failed_sales_transaction_id = None
    if result.order_id:
        try:
            order_uuid = UUID(result.order_id)
        except ValueError:
            order_uuid = None

        if order_uuid is not None:
            init_txn = None
            order = db.query(Sale).filter(
                Sale.id == order_uuid,
                Sale.tenant_id == tenant_id,
            ).first()

            # When we only log initiation in sales_transactions, the Sale is created here on success.
            if order is None:
                init_txn = (
                    db.query(SalesTransactions)
                    .filter(
                        SalesTransactions.tenant_id == tenant_id,
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
                    duplicate_one_time = bool(
                        pkg_raw
                        and PackagesService.is_one_time_duplicate_purchase(
                            db,
                            tenant_id=tenant_id,
                            user_id=init_txn.user_id,
                            package_id=UUID(str(pkg_raw)),
                        )
                    )
                    if not duplicate_one_time:
                        order = Sale(
                            id=order_uuid,
                            tenant_id=tenant_id,
                            user_id=init_txn.user_id,
                            package_id=UUID(str(pkg_raw)) if pkg_raw else None,
                            product_item_type="package",
                            type="gateway",
                            wallet_transaction_id=None,
                            amount=init_txn.amount or 0,
                        )
                        db.add(order)
                        db.flush()
                        init_txn.sales_id = order.id

            audit_sale = order or db.query(Sale).filter(Sale.id == order_uuid).first()
            gateway_status = _wallet_status_from_gateway(result.status)

            if order:
                if order.package_id is not None:
                    apply_package_expiry_to_sale(
                        db, order, tenant_id, overwrite=True
                    )

                if gateway_status in ("succeeded", "success"):
                    if order.package_id is not None and PackagesService.is_one_time_duplicate_purchase(
                        db,
                        tenant_id=tenant_id,
                        user_id=order.user_id,
                        package_id=order.package_id,
                        exclude_sale_id=order.id,
                    ):
                        gateway_status = "failed"
                    else:
                        existing_user_package = (
                            db.query(UserPackage)
                            .filter(UserPackage.sale_id == order.id)
                            .first()
                        )
                        init_meta = init_txn.extra_metadata if init_txn is not None else None
                        user_package = ensure_user_package_for_completed_package_sale(
                            db,
                            order,
                            created_by=(
                                init_txn.created_by_type if init_txn is not None else "member"
                            ),
                            created_by_id=(
                                init_txn.created_by_id if init_txn is not None else order.user_id
                            ),
                            snapshot=init_meta if isinstance(init_meta, dict) else None,
                        )
                        if user_package and existing_user_package is None:
                            package_purchased_user_package_id = user_package.id

            # Prefer updating the initiation ("created") row instead of inserting a second one.
            init_pkg_txn = (
                db.query(SalesTransactions)
                .filter(
                    SalesTransactions.tenant_id == tenant_id,
                    SalesTransactions.source == "package",
                    SalesTransactions.extra_metadata["client_order_id"].astext == str(order_uuid),
                    SalesTransactions.extra_metadata["event"].astext == "created",
                )
                .order_by(SalesTransactions.created_at.desc())
                .first()
            )

            status_value = sales_transaction_status_from_gateway(result.status)
            gateway_value = (
                result.gateway.value if hasattr(result.gateway, "value") else str(result.gateway)
            ) or ""

            if init_pkg_txn is not None:
                previous_pkg_status = init_pkg_txn.status
                init_pkg_txn.sales_id = order.id if order else order_uuid
                init_pkg_txn.gateway = gateway_value
                init_pkg_txn.gateway_txn_id = result.transaction_id or init_pkg_txn.gateway_txn_id or ""
                init_pkg_txn.status = status_value
                init_pkg_txn.amount = result.amount or init_pkg_txn.amount
                init_pkg_txn.currency = (result.currency or init_pkg_txn.currency or default_currency)
                meta = dict(init_pkg_txn.extra_metadata or {})
                meta.setdefault("event", "created")
                meta["resolved_by"] = "callback"
                meta["last_event"] = "callback"
                init_pkg_txn.extra_metadata = meta
                db.flush()
                if order is not None:
                    order.provider_numeric_transaction_id = init_pkg_txn.id
                if previous_pkg_status not in TERMINAL_SALES_TRANSACTION_STATUSES:
                    if status_value == SalesTransactionStatus.cancelled:
                        payment_failed_sales_transaction_id = str(init_pkg_txn.id)
                db.commit()
            else:
                txn = SalesTransactions(
                    sales_id=order.id if order else order_uuid,
                    tenant_id=tenant_id,
                    location_id=LocationsService.resolve_location_id(db, tenant_id),
                    payment_method="gateway",
                    gateway=gateway_value,
                    gateway_txn_id=result.transaction_id or "",
                    source="package",
                    status=status_value,
                    amount=result.amount,
                    currency=result.currency,
                    user_id=order.user_id if order else (audit_sale.user_id if audit_sale else None),
                    created_by_type=(
                        getattr(init_pkg_txn, "created_by_type", None)
                        or (init_txn.created_by_type if init_txn is not None else None)
                        or "gateway"
                    ),
                    created_by_id=(
                        (audit_sale.user_id if audit_sale else None)
                        or (order.user_id if order else None)
                    ),
                    extra_metadata={"event": "callback"},
                )
                db.add(txn)
                db.flush()
                if order is not None:
                    order.provider_numeric_transaction_id = txn.id
                if status_value == SalesTransactionStatus.cancelled:
                    payment_failed_sales_transaction_id = str(txn.id)
                db.commit()

    # ----------------------------------------------------------------
    # Persist result to wallet transactions (top-ups)
    # ----------------------------------------------------------------
    wallet_topup_txn_id = None
    wallet_topup_failed_txn_id = None
    if result.transaction_id:
        wallet_txn = (
            db.query(WalletTransaction)
            .filter(WalletTransaction.transaction_id == result.transaction_id)
            .first()
        )
    else:
        wallet_txn = None

    # If wallet top-up initiation only logged sales_transactions, create wallet_transactions + sales here.
    if wallet_txn is None and result.transaction_id:
        init_wallet_txn = (
            db.query(SalesTransactions)
            .filter(
                SalesTransactions.source == "wallet",
                SalesTransactions.gateway_txn_id == result.transaction_id,
                SalesTransactions.extra_metadata["event"].astext == "created",
            )
            .order_by(SalesTransactions.created_at.desc())
            .first()
        )
        if init_wallet_txn and init_wallet_txn.user_id:
            user = db.query(User).filter(User.id == init_wallet_txn.user_id).first()
            before = float(user.wallet or 0) if user else 0.0
            credited = float(init_wallet_txn.amount or 0)
            after = before + credited
            previous_wallet_status = init_wallet_txn.status

            wallet_txn = WalletTransaction(
                user_id=init_wallet_txn.user_id,
                direction="credit",
                transaction_id=result.transaction_id,
                amount=init_wallet_txn.amount or 0,
                currency=(init_wallet_txn.currency or (result.currency or default_currency)).upper(),
                balance_before=before,
                balance_after=after if _wallet_status_from_gateway(result.status) == "succeeded" else None,
                created_by=init_wallet_txn.created_by_type,
                created_by_id=init_wallet_txn.created_by_id,
                reference_type="wallet_add",
            )
            db.add(wallet_txn)
            db.flush()

            sale = Sale(
                tenant_id=init_wallet_txn.tenant_id,
                user_id=init_wallet_txn.user_id,
                package_id=wallet_txn.id,
                product_item_type="wallet",
                type="gateway",
                wallet_transaction_id=wallet_txn.id,
                amount=init_wallet_txn.amount or 0,
            )
            db.add(sale)
            db.flush()
            wallet_txn.reference_id = sale.id
            init_wallet_txn.sales_id = sale.id

            # Update the initiation row instead of inserting a second sales_transactions row.
            init_wallet_txn.sales_id = sale.id
            init_wallet_txn.status = sales_transaction_status_from_gateway(result.status)
            init_wallet_txn.gateway = (
                result.gateway.value if hasattr(result.gateway, "value") else str(result.gateway)
            )
            init_wallet_txn.currency = (result.currency or init_wallet_txn.currency or default_currency)
            init_wallet_txn.amount = result.amount or init_wallet_txn.amount or sale.amount
            meta = dict(init_wallet_txn.extra_metadata or {})
            meta.setdefault("event", "created")
            meta["resolved_by"] = "callback"
            init_wallet_txn.extra_metadata = meta
            sale.provider_numeric_transaction_id = init_wallet_txn.id

            gateway_status = _wallet_status_from_gateway(result.status)
            # Credit user wallet only on success.
            if user and gateway_status == "succeeded":
                user.wallet = after
                wallet_topup_txn_id = wallet_txn.id
            elif (
                gateway_status == "cancelled"
                and previous_wallet_status not in TERMINAL_SALES_TRANSACTION_STATUSES
            ):
                payment_failed_sales_transaction_id = str(init_wallet_txn.id)
            elif (
                gateway_status == "failed"
                and previous_wallet_status not in TERMINAL_SALES_TRANSACTION_STATUSES
            ):
                wallet_topup_failed_txn_id = wallet_txn.id

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

        # Idempotency: don't double-credit or re-notify terminal states.
        if wallet_txn.status != "succeeded":
            previous_status = wallet_txn.status
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
                    wallet_topup_txn_id = wallet_txn.id
            elif (
                new_status == "cancelled"
                and previous_status not in ("failed", "cancelled", "succeeded")
                and wallet_txn.direction == "credit"
                and wallet_txn.transaction_type == "wallet_add"
            ):
                st = (
                    db.query(SalesTransactions)
                    .filter(
                        SalesTransactions.gateway_txn_id == result.transaction_id,
                        SalesTransactions.source == "wallet",
                    )
                    .order_by(SalesTransactions.created_at.desc())
                    .first()
                )
                if st is not None:
                    payment_failed_sales_transaction_id = str(st.id)
                elif wallet_txn.linked_sale and wallet_txn.linked_sale.provider_numeric_transaction_id:
                    payment_failed_sales_transaction_id = str(
                        wallet_txn.linked_sale.provider_numeric_transaction_id
                    )
            elif (
                new_status == "failed"
                and previous_status not in ("failed", "cancelled", "succeeded")
                and wallet_txn.direction == "credit"
                and wallet_txn.transaction_type == "wallet_add"
            ):
                wallet_topup_failed_txn_id = wallet_txn.id

            db.commit()

    if wallet_topup_txn_id is not None:
        sale = db.query(Sale).filter(Sale.wallet_transaction_id == wallet_topup_txn_id).first()
        event_tenant_id = event_tenant_id_from_sale(sale, tenant_id)
        if sale is not None and not wallet_topup_email_pending(sale):
            logger.info(
                "wallet_topup_pubsub_skipped_already_sent tenant_id=%s event_tenant_id=%s wallet_transaction_id=%s",
                tenant_id,
                event_tenant_id,
                wallet_topup_txn_id,
            )
        else:
            published = await WalletNotificationService.publish_topup_success(
                db,
                tenant_id=event_tenant_id,
                wallet_transaction_id=wallet_topup_txn_id,
            )
            if published is not None and sale is not None:
                mark_wallet_topup_email_sent(sale)
                db.commit()
    if wallet_topup_failed_txn_id is not None:
        failed_sale = (
            db.query(Sale)
            .filter(Sale.wallet_transaction_id == wallet_topup_failed_txn_id)
            .first()
        )
        await WalletNotificationService.publish_topup_failed(
            db,
            tenant_id=event_tenant_id_from_sale(failed_sale, tenant_id),
            wallet_transaction_id=wallet_topup_failed_txn_id,
        )
    if package_purchased_user_package_id is not None:
        await PackageNotificationService.publish_purchased(
            db,
            tenant_id=event_tenant_id_from_user_package_id(
                db, package_purchased_user_package_id, tenant_id
            ),
            user_package_id=package_purchased_user_package_id,
        )
    if payment_failed_sales_transaction_id is not None:
        failed_st = (
            db.query(SalesTransactions)
            .filter(SalesTransactions.id == int(payment_failed_sales_transaction_id))
            .first()
        )
        failed_sale = None
        if failed_st and failed_st.sales_id:
            failed_sale = db.query(Sale).filter(Sale.id == failed_st.sales_id).first()
        await PaymentNotificationService.publish_failed(
            db,
            tenant_id=event_tenant_id_from_sale(failed_sale, tenant_id),
            sales_transaction_id=payment_failed_sales_transaction_id,
        )

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
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    include_wallet_add: bool = Query(False, description="Include wallet top-ups in results"),
):
    """
    Cash-revenue transaction history: gateway package purchases and optional wallet top-ups.
    Wallet-balance package purchases are excluded (see GET /wallet/transactions/purchases).
    """
    base_query = (
        db.query(Sale)
        .filter(
            Sale.user_id == current_user.id,
            Sale.tenant_id == tenant_id,
            (
                (Sale.type == "package_gateway")
                | ((Sale.type == "gateway") & (Sale.product_item_type == "package"))
            )
            | (
                include_wallet_add
                & (Sale.product_item_type == "wallet")
                & (Sale.type == "gateway")
            ),
        )
        .order_by(Sale.created_at.desc())
    )
    total = base_query.count()
    offset = (page - 1) * limit
    sales = base_query.offset(offset).limit(limit).all()
    sale_ids = [s.id for s in sales]
    latest_st_by_sale: dict = {}
    if sale_ids:
        st_rows = (
            db.query(SalesTransactions)
            .filter(SalesTransactions.sales_id.in_(sale_ids))
            .order_by(SalesTransactions.created_at.desc())
            .all()
        )
        for st in st_rows:
            if st.sales_id not in latest_st_by_sale:
                latest_st_by_sale[st.sales_id] = st

    def _row(sale: Sale) -> dict[str, Any]:
        st = latest_st_by_sale.get(sale.id)
        is_package_purchase = (sale.type in ("package_gateway", "package_wallet")) or (
            sale.type == "gateway" and sale.product_item_type == "package"
        ) or (
            sale.type == "wallet" and sale.product_item_type == "package"
        )
        return {
            "id": st.id if st else sale.id,
            "sales_id": sale.id,
            "type": sale.type,
            "payment_method": "wallet"
            if sale.type in ("package_wallet", "wallet")
            else "gateway",
            "purchase_source": (
                "wallet_topup"
                if (sale.product_item_type == "wallet")
                else (
                    "wallet_purchase"
                    if sale.type in ("package_wallet", "wallet")
                    else "gateway_purchase"
                )
            ),
            "is_package_purchase": is_package_purchase,
            "gateway": st.gateway if st else sale_gateway_value(db, sale),
            "gateway_txn_id": st.gateway_txn_id if st else sale_gateway_txn_id(db, sale),
            "txn_ref": st.txn_ref if st else None,
            "status": normalize_display_status(
                st.status.value if st else sale_status_value(db, sale)
            ),
            "amount": st.amount if st is not None and st.amount is not None else sale.amount,
            "currency": st.currency if st is not None and st.currency is not None else sale_currency_value(db, sale),
            "package_id": sale.package_id,
            "pricing_id": sale_pricing_id(db, sale),
            "wallet_transaction_id": sale.wallet_transaction_id,
            "created_at": st.created_at if st else sale.created_at,
        }

    return {
        "success": True,
        "message": "Sales transactions fetched successfully",
        "data": [_row(sale) for sale in sales],
        "count": len(sales),
        "pagination": build_pagination(page, limit, total),
    }


@router.get("/gateways")
async def list_active_gateways(
    tenant_id: str = Depends(get_tenant_id),
    current_user: User = Depends(get_current_active_user),
):
    """
    Return which payment gateways are configured for the current tenant.
    Requires authentication. Useful for apps that want to show multiple payment options
    (e.g. Stripe / PayPal) based on tenant setup.
    """
    try:
        settings = TenantPaymentSettings.get(tenant_id)
        configured = list(settings.get("gateways", {}).keys())
        active = settings.get("active_gateway")
    except ValueError:
        # Treat "no configured gateways" as a valid state for new tenants.
        configured = []
        active = None

    return {
        "active_gateway":      active,
        "configured_gateways": configured,
    }