"""Structured logging and request context, shared by the FastAPI services.

Every log record is one JSON object on stdout, the shape Cloud Logging and Error Reporting
read natively: ``severity``, ``message``, ``time``, plus ``service``, ``version``,
``request_id``, the Cloud Trace id (``logging.googleapis.com/trace``), ``tenant_id`` and
``user_id`` of the request being served, and ``stack_trace`` for exceptions.
``LOG_FORMAT=text`` prints one readable line instead (local development).

RequestContextMiddleware (outermost) takes ``X-Request-ID`` from the caller or makes one,
returns it on the response, and writes one access line per request (method, route,
status, latency). Code that knows the tenant or user calls ``bind_request(...)`` so every
later log line of that request carries them. Pub/Sub publishers send ``current_request_id()``
as a message attribute, so a consumer can log under the same id.

Secrets never reach the output: bearer tokens, JWTs and ``password= / token= / otp= ...``
values are replaced, emails and phone numbers are masked (also inside stack traces).

This file is the same in every service; keep the copies in sync.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

_request_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)
_trace: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("trace", default=None)
_tenant_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("tenant_id", default=None)
_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("user_id", default=None)

_SERVICE = "app"
_VERSION = os.getenv("K_REVISION") or os.getenv("GIT_SHA") or "dev"
_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID") or ""
_SKIP_ACCESS_LOG = ("/health", "/ready", "/live", "/favicon.ico")
_REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


# ---------------------------------------------------------------- request context
def current_request_id() -> Optional[str]:
    return _request_id.get()


def bind_request(*, tenant_id: Any = None, user_id: Any = None) -> None:
    """Attach the tenant / user to the rest of this request's logs (None keeps the value)."""
    if tenant_id:
        _tenant_id.set(str(tenant_id))
    if user_id:
        _user_id.set(str(user_id))


def bind_message(*, request_id: Optional[str] = None, tenant_id: Any = None) -> list:
    """For consumers: log under the publisher's request id. Returns tokens for unbind_message."""
    return [_request_id.set(request_id or uuid.uuid4().hex), _tenant_id.set(str(tenant_id) if tenant_id else None)]


def unbind_message(tokens: list) -> None:
    for var, token in zip((_request_id, _tenant_id), tokens):
        var.reset(token)


def _header(scope: dict, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


class RequestContextMiddleware:
    """Pure ASGI (no extra task per request). Add it last, so it wraps everything else."""

    def __init__(self, app):
        self.app = app
        self.access = logging.getLogger("access")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = _header(scope, b"x-request-id")
        request_id = incoming if incoming and _REQUEST_ID_OK.match(incoming) else uuid.uuid4().hex
        trace = None
        cloud_trace = _header(scope, b"x-cloud-trace-context")
        if cloud_trace and _PROJECT:
            trace = f"projects/{_PROJECT}/traces/{cloud_trace.split('/', 1)[0]}"
        tokens = [_request_id.set(request_id), _trace.set(trace), _tenant_id.set(None), _user_id.set(None)]
        scope.setdefault("state", {})["request_id"] = request_id
        status = 500
        start = time.perf_counter()

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            path = scope.get("path", "")
            if not path.endswith(_SKIP_ACCESS_LOG):
                state = scope.get("state") or {}
                bind_request(tenant_id=state.get("tenant_id"), user_id=state.get("user_id"))
                route = scope.get("route")
                self.access.log(
                    logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO,
                    "%s %s %s", scope.get("method"), path, status,
                    extra={"fields": {
                        "http": {"method": scope.get("method"), "path": path, "status": status,
                                 "route": getattr(route, "path", None),
                                 "latency_ms": round((time.perf_counter() - start) * 1000, 1)}}},
                )
            for var, token in zip((_request_id, _trace, _tenant_id, _user_id), tokens):
                var.reset(token)


# ---------------------------------------------------------------- redaction
_REDACT = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), "[jwt]"),
    (re.compile(r"(?i)([\"']?\b(?:password|passwd|pwd|secret|client_secret|token|access_token|refresh_token|"
                r"api_key|apikey|otp|otp_code|authorization)\b[\"']?\s*[:=]\s*[\"']?)[^\"'\s,&;}]+"), r"\1[redacted]"),
    (re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"), r"\1***@\2"),
    (re.compile(r"\+\d{5,12}(\d{3})\b"), r"+***\1"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACT:
        text = pattern.sub(replacement, text)
    return text


def redact_values(value: Any) -> Any:
    """Redact every string inside a log field (dicts and lists included)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_values(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_values(v) for v in value]
    return value


# ---------------------------------------------------------------- formatters
def _context() -> dict:
    out = {}
    for key, var in (("request_id", _request_id), ("tenant_id", _tenant_id), ("user_id", _user_id)):
        value = var.get()
        if value:
            out[key] = value
    if _trace.get():
        out["logging.googleapis.com/trace"] = _trace.get()
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": record.levelname if record.levelname != "WARN" else "WARNING",
            "message": redact(record.getMessage()),
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "logger": record.name,
            "service": _SERVICE,
            "version": _VERSION,
            **_context(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(redact_values(fields))
        if record.exc_info:
            entry["stack_trace"] = redact("".join(traceback.format_exception(*record.exc_info)))
        elif record.stack_info:
            entry["stack_trace"] = redact(record.stack_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rid = _request_id.get()
        line = (f"{datetime.fromtimestamp(record.created).strftime('%H:%M:%S')} {record.levelname:<7} "
                f"{record.name}{' [' + rid[:8] + ']' if rid else ''} {redact(record.getMessage())}")
        if record.exc_info:
            line += "\n" + redact("".join(traceback.format_exception(*record.exc_info)))
        return line


def configure_logging(service: str) -> None:
    """Route every logger (uvicorn's too) to one stdout handler in the chosen format."""
    global _SERVICE
    _SERVICE = os.getenv("K_SERVICE") or service
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TextFormatter() if os.getenv("LOG_FORMAT", "json").lower() == "text" else JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    for name in ("uvicorn", "uvicorn.error", "fastapi"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    # RequestContextMiddleware writes the access line (with request id and latency).
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
