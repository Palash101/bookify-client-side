from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.orm import Session
from app.core.db.session import SessionLocal
from app.models.tenant_api_key import TenantAPIKey
from app.models.tenant import Tenant
import time
import logging

logger = logging.getLogger(__name__)

EXCLUDED_PATHS = [
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/openapi.json",
]


class TenantMiddleware(BaseHTTPMiddleware):
    """
    Middleware to validate X-Tenant-Key header for all API requests.
    Excludes public paths like /, /health, /docs, etc.
    """
    
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        
        if request.method == "OPTIONS":
            return await call_next(request)
        
        if path in EXCLUDED_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
            return await call_next(request)
        
        if not path.startswith("/api/"):
            return await call_next(request)
        
        x_tenant_key = request.headers.get("X-Tenant-Key")
        
        if not x_tenant_key:
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "message": "X-Tenant-Key header is required",
                    "detail": "X-Tenant-Key header is required"
                }
            )
        
        db: Session = SessionLocal()
        try:
            api_key = (
                db.query(TenantAPIKey)
                .filter(
                    TenantAPIKey.api_key_hash == x_tenant_key,
                    TenantAPIKey.is_active.is_(True),
                )
                .first()
            )
            
            if not api_key:
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "Invalid or inactive tenant API key",
                        "detail": "Invalid or inactive tenant API key"
                    }
                )
            
            tenant = (
                db.query(Tenant)
                .filter(
                    Tenant.id == api_key.tenant_id,
                    Tenant.status == "active",
                )
                .first()
            )
            
            if not tenant:
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "Tenant not found or inactive",
                        "detail": "Tenant not found or inactive"
                    }
                )
            
            request.state.tenant_id = tenant.id
            request.state.tenant = tenant
            
        finally:
            db.close()
        
        return await call_next(request)


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware for logging HTTP requests.
    """
    
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        
        logger.info(f"Request: {request.method} {request.url.path}")
        
        response = await call_next(request)
        
        process_time = time.time() - start_time
        
        logger.info(
            f"Response: {response.status_code} - "
            f"Process time: {process_time:.4f}s"
        )
        
        response.headers["X-Process-Time"] = str(process_time)
        
        return response


class CORSMiddleware(BaseHTTPMiddleware):
    """
    Custom CORS middleware if needed.
    """
    
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response
