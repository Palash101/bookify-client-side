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
from app.models.sales import Sale
from app.services.sale_expiry import apply_package_expiry_to_sale
from app.models.sales_transactions import SalesTransactions
from app.models.wallet_transactions import WalletTransaction
from app.models.user import User

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
    def _redirect_to_app(**query: Optional[str]) -> RedirectResponse:
        base = settings.PAYMENT_SUCCESS_DEEP_LINK.rstrip("/")
        q = {k: str(v) for k, v in query.items() if v is not None and str(v) != ""}
        url = f"{base}?{urlencode(q)}" if q else base
        return RedirectResponse(url=url, status_code=302)

    sale = None
    wallet_txn = None

    if session_id:
        db = SessionLocal()
        try:
            sale = db.query(Sale).filter(Sale.gateway_transaction_id == session_id).first()
            wallet_txn = db.query(WalletTransaction).filter(
                WalletTransaction.transaction_id == session_id
            ).first()

            if sale and sale.status != "succeeded":
                sale.status = "succeeded"
                if sale.package_id is not None:
                    apply_package_expiry_to_sale(db, sale, sale.tenant_id, overwrite=False)
                sale_log = SalesTransactions(
                    order_id=sale.id,
                    tenant_id=sale.tenant_id,
                    type=sale.type or "package_gateway",
                    gateway=sale.gateway,
                    gateway_txn_id=session_id,
                    event_type="success_redirect",
                    status="succeeded",
                    amount=sale.amount,
                    currency=sale.currency,
                    raw_payload={"source": "payment_success_redirect", "session_id": session_id},
                )
                db.add(sale_log)

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

            db.commit()
        finally:
            db.close()

        return _redirect_to_app(session_id=session_id)

    return _redirect_to_app(error="missing_session_id")


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
