from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from urllib.parse import urlencode
from fastapi.exceptions import RequestValidationError
from fastapi import HTTPException
from fastapi.openapi.utils import get_openapi
from typing import Optional
from app.core.settings import settings
from app.core.middleware import TenantMiddleware
from app.api import api_router
from app.core.db.session import SessionLocal
from app.models.sales import Sale, backfill_sale_checkout_metadata, SALE_WALLET_TXN_KEY
from app.services.sale_expiry import apply_package_expiry_to_sale
from app.services.user_package_service import ensure_user_package_for_completed_package_sale
from app.models.sales_transactions import SalesTransactions
from app.models.wallet_transactions import WalletTransaction
from app.models.user import User
from uuid import UUID
import logging

logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    swagger_ui_parameters={"persistAuthorization": True}
)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    
    openapi_schema = get_openapi(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description="Bookify API - Multi-tenant booking platform",
        routes=app.routes,
    )
    
    openapi_schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Enter your verification token (from login/register) or access token (from verify-otp)"
        },
        "TenantKey": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Tenant-Key",
            "description": "Tenant API Key (required for all /api/* endpoints)"
        }
    }
    
    endpoints_needing_bearer = [
        "/api/v1/auth/verify-otp",
        "/api/v1/auth/reset-password",
        "/api/v1/auth/profile",
        "/api/v1/auth/edit-profile",
    ]
    
    for path, path_item in openapi_schema.get("paths", {}).items():
        if path.startswith("/api/"):
            for method in path_item.values():
                if isinstance(method, dict):
                    security = [{"TenantKey": []}]
                    
                    if (
                        path in endpoints_needing_bearer
                        or path.endswith("/profile")
                        or "/bookings" in path
                    ):
                        security.append({"BearerAuth": []})
                    
                    existing_security = method.get("security", [])
                    for sec in existing_security:
                        if "BearerAuth" in sec and {"BearerAuth": []} not in security:
                            security.append({"BearerAuth": []})
                    
                    method["security"] = security
    
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


# Custom exception handler for HTTPException
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """
    Custom handler to format HTTPException responses with success and message fields.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "message": exc.detail,
            "detail": exc.detail
        }
    )

# Custom exception handler for validation errors
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Custom handler to format validation errors with success and message fields.
    """
    errors = exc.errors()
    error_messages = []
    for error in errors:
        field = " -> ".join(str(loc) for loc in error.get("loc", []))
        msg = error.get("msg", "Validation error")
        error_messages.append(f"{field}: {msg}")
    
    message = "Validation error. " + "; ".join(error_messages) if error_messages else "Validation error"
    
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "success": False,
            "message": message,
            "detail": errors
        }
    )

# CORS middleware (must be first)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Tenant validation middleware
app.add_middleware(TenantMiddleware)

# Include API router
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


# ---------------------------------------------------------------------------
# Payment redirect endpoints (no /api/v1 prefix)
# Gateways may redirect users to /payment/success or /payment/cancel.
# Webhooks/callbacks are handled under /api/v1/payment/callback/{gateway_type}.
# ---------------------------------------------------------------------------

@app.get("/payment/success")
async def payment_success(session_id: Optional[str] = None):
    # Temporarily disable deep-link redirect (PAYMENT_SUCCESS_DEEP_LINK) and return JSON instead.
    def _respond(**payload: Optional[str]) -> JSONResponse:
        clean = {k: str(v) for k, v in payload.items() if v is not None and str(v) != ""}
        return JSONResponse(
            status_code=200,
            content={
                "success": "error" not in clean,
                "message": clean.get("error") or "Payment success received",
                **clean,
            },
        )

    sale = None
    wallet_txn = None
    debug: dict[str, str] = {}

    if session_id:
        db = SessionLocal()
        try:
            try:
                sale = db.query(Sale).filter(Sale.gateway_transaction_id == session_id).first()
                debug["sale_found_by_session"] = "1" if sale is not None else "0"

                # If we didn't create Sale at initiation, derive it from the initiation SalesTransactions row.
                if sale is None and (session_id or "").startswith("cs_"):
                    init_txn = (
                        db.query(SalesTransactions)
                        .filter(
                            SalesTransactions.source == "package",
                            SalesTransactions.gateway_txn_id == session_id,
                            SalesTransactions.extra_metadata["event"].astext == "created",
                        )
                        .order_by(SalesTransactions.created_at.desc())
                        .first()
                    )
                    debug["init_txn_found_by_gateway_txn_id"] = "1" if init_txn is not None else "0"
                    if init_txn is not None:
                        debug["init_txn_id"] = str(getattr(init_txn, "id", "") or "")
                    if init_txn and init_txn.user_id and isinstance(init_txn.extra_metadata, dict):
                        meta = init_txn.extra_metadata or {}
                        client_order_id = meta.get("client_order_id")
                        if client_order_id:
                            order_uuid = UUID(str(client_order_id))
                            pkg_raw = meta.get("package_id")
                            pricing_raw = meta.get("package_pricing_id")
                            sale = Sale(
                                id=order_uuid,
                                tenant_id=init_txn.tenant_id,
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
                                    "currency": init_txn.currency or "QAR",
                                    "gateway": "stripe",
                                    "status": "succeeded",
                                    "gateway_transaction_id": session_id,
                                },
                            )
                            db.add(sale)
                            db.flush()
                            init_txn.order_id = sale.id
                            debug["sale_created_from_init_txn"] = "1"
                            debug["sale_id"] = str(sale.id)
                    # If there's no initiation row, we can't create Sale/UserPackage in this flow.
                    if sale is None and debug.get("init_txn_found_by_gateway_txn_id") == "0":
                        # Try wallet top-up initiation lookup too.
                        init_wallet = (
                            db.query(SalesTransactions)
                            .filter(
                                SalesTransactions.source == "wallet",
                                SalesTransactions.gateway_txn_id == session_id,
                                SalesTransactions.extra_metadata["event"].astext == "created",
                            )
                            .order_by(SalesTransactions.created_at.desc())
                            .first()
                        )
                        debug["init_wallet_txn_found_by_gateway_txn_id"] = "1" if init_wallet is not None else "0"
                        if init_wallet and init_wallet.user_id and isinstance(init_wallet.extra_metadata, dict):
                            user = db.query(User).filter(User.id == init_wallet.user_id).first()
                            before = float(user.wallet or 0) if user else 0.0
                            credited = float(init_wallet.amount or 0)
                            after = before + credited

                            wtxn = WalletTransaction(
                                user_id=init_wallet.user_id,
                                direction="credit",
                                transaction_id=session_id,
                                amount=init_wallet.amount or 0,
                                currency=(init_wallet.currency or "QAR").upper(),
                                balance_before=before,
                                balance_after=after,
                                created_by=init_wallet.created_by_type,
                                created_by_id=init_wallet.created_by_id,
                            )
                            db.add(wtxn)
                            db.flush()

                            sale = Sale(
                                tenant_id=init_wallet.tenant_id,
                                user_id=init_wallet.user_id,
                                package_id=None,
                                product_item_type=None,
                                type="wallet_add",
                                created_by_type=init_wallet.created_by_type,
                                created_by_id=init_wallet.created_by_id,
                                wallet_transaction_id=wtxn.id,
                                amount=init_wallet.amount or 0,
                                extra_metadata={
                                    "purpose": "wallet_add",
                                    "currency": (init_wallet.currency or "QAR").upper(),
                                    "gateway": "stripe",
                                    "status": "succeeded",
                                    "gateway_transaction_id": session_id,
                                    SALE_WALLET_TXN_KEY: {
                                        "transaction_type": "wallet_add",
                                        "status": "succeeded",
                                        "tenant_id": str(init_wallet.tenant_id),
                                    },
                                },
                            )
                            db.add(sale)
                            db.flush()
                            init_wallet.order_id = sale.id
                            if user:
                                user.wallet = after
                            debug["sale_created_from_init_wallet_txn"] = "1"
                            debug["sale_id"] = str(sale.id)
                        if sale is None:
                            db.rollback()
                            return _respond(
                                error="missing_initiation_sales_transaction",
                                session_id=session_id,
                                **debug,
                            )
            except Exception:
                logger.exception("payment_success reconciliation failed (session_id=%s)", session_id)
                db.rollback()
                return _respond(error="payment_success_failed", session_id=session_id, **debug)
            # Stripe Checkout session ids (cs_…) live on the sale for packages; wallet ledger uses the same id only for wallet top-ups.
            wallet_txn = None
            if sale is None or (sale.type or "") == "wallet_add":
                wallet_txn = (
                    db.query(WalletTransaction)
                    .filter(WalletTransaction.transaction_id == session_id)
                    .first()
                )

            if sale:
                backfill_sale_checkout_metadata(sale, session_id)
                st = (sale.status or "").lower()
                if st not in ("succeeded", "success"):
                    sale.status = "succeeded"
                    st = "succeeded"
                if sale.package_id is not None:
                    apply_package_expiry_to_sale(db, sale, sale.tenant_id, overwrite=False)
                if st in ("succeeded", "success"):
                    ensure_user_package_for_completed_package_sale(
                        db,
                        sale,
                        created_by=sale.created_by_type or "member",
                        created_by_id=sale.created_by_id or sale.user_id,
                    )
                    # Webhook may complete the sale first; still record redirect once (no duplicate rows).
                    if not (
                        db.query(SalesTransactions)
                        .filter(
                            SalesTransactions.order_id == sale.id,
                            SalesTransactions.source == ("wallet" if (sale.type or "") == "wallet_add" else "package"),
                        )
                        .first()
                    ):
                        src = "wallet" if (sale.type or "") == "wallet_add" else "package"
                        pay_method = "gateway" if src == "wallet" else "gateway"
                        st_norm = "success" if (sale.status or "").lower() in ("succeeded", "success") else "failed"
                        sale_txn = SalesTransactions(
                            order_id=sale.id,
                            tenant_id=sale.tenant_id,
                            payment_method=pay_method,
                            gateway=sale.gateway
                            or (sale.extra_metadata or {}).get("gateway")
                            or ("stripe" if (session_id or "").startswith("cs_") else ""),
                            gateway_txn_id=session_id,
                            source=src,
                            status=st_norm,
                            amount=sale.amount,
                            currency=sale.currency,
                            user_id=sale.user_id,
                            created_by_type=sale.created_by_type or "member",
                            created_by_id=sale.created_by_id or sale.user_id,
                            extra_metadata={"event": "success_redirect"},
                        )
                        db.add(sale_txn)
                        db.flush()
                        sale.provider_numeric_transaction_id = sale_txn.id

            if (
                wallet_txn
                and wallet_txn.status != "succeeded"
                and wallet_txn.direction == "credit"
                and wallet_txn.transaction_type == "wallet_add"
            ):
                user = db.query(User).filter(User.id == wallet_txn.user_id).first()
                if user:
                    before = float(user.wallet or 0)
                    credit_amount = float(wallet_txn.amount or 0)
                    after = before + credit_amount
                    user.wallet = after
                    wallet_txn.status = "succeeded"
                    wallet_txn.balance_before = before
                    wallet_txn.balance_after = after
                    ls = wallet_txn.linked_sale
                    if ls:
                        ls.status = "succeeded"

            db.commit()
        finally:
            db.close()

        if sale is not None:
            debug.setdefault("sale_id", str(sale.id))
            debug.setdefault("sale_status", str(sale.status))
        return _respond(session_id=session_id, **debug)

    return _respond(error="missing_session_id")


@app.get("/payment/cancel")
async def payment_cancel(session_id: Optional[str] = None):
    return {
        "success": False,
        "message": "Payment cancelled redirect received",
        "session_id": session_id,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
