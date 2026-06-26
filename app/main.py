from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi import HTTPException
from fastapi.openapi.utils import get_openapi
from typing import Optional
from app.core.settings import settings
from app.core.middleware import TenantMiddleware
from app.api import api_router
from app.payments.redirect_handlers import (
    build_payment_cancel_response,
    build_payment_success_response,
)
import logging

logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url=f"{settings.API_V1_STR}/docs",
    redoc_url=f"{settings.API_V1_STR}/redoc",
    swagger_ui_parameters={"persistAuthorization": True},
)


@app.on_event("startup")
async def log_pubsub_startup_config() -> None:
    mode = "console" if settings.publisher_is_console else "gcp"
    logger.info(
        "pubsub_startup mode=%s project=%s topic=%s ordering=%s",
        mode,
        settings.GCP_PROJECT_ID or "(none)",
        settings.PUBSUB_TOPIC_ID,
        settings.PUBSUB_ENABLE_MESSAGE_ORDERING,
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
    
    api_prefix = settings.API_V1_STR.rstrip("/")
    endpoints_needing_bearer = [
        f"{api_prefix}/auth/verify-otp",
        f"{api_prefix}/auth/reset-password",
        f"{api_prefix}/auth/profile",
        f"{api_prefix}/auth/edit-profile",
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
# Legacy payment redirect paths (no /api/v1/client prefix).
# Prefer /api/v1/client/payment/success and /api/v1/client/payment/cancel.
# ---------------------------------------------------------------------------

@app.get("/payment/success")
async def payment_success_legacy(session_id: Optional[str] = None):
    return await build_payment_success_response(session_id)


@app.get("/payment/cancel")
async def payment_cancel_legacy(session_id: Optional[str] = None):
    return await build_payment_cancel_response(session_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
