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

Tracing (``init_tracing``) is on when ``TRACE_SAMPLE_RATE`` > 0: requests, SQL, Redis and
outgoing HTTP become spans sent to Cloud Trace (on Cloud Run) or an OTLP collector such as
Jaeger (``TRACE_EXPORTER=otlp``, ``OTEL_EXPORTER_OTLP_ENDPOINT``). Pub/Sub messages carry
the trace (``trace_carrier`` / ``consumer_span``), so one booking is one trace across the
API and the activities worker; log lines carry its trace and span id.

Errors go to Sentry when ``SENTRY_DSN`` is set (``init_error_tracking``): unhandled exceptions
and ``logger.error`` / ``logger.exception``, tagged with request_id, tenant_id, user id and
release ``<service>@<version>``; no headers, cookies or IPs, same redaction. Without the
DSN nothing is sent and the package is not even imported.

This file is the same in every service; keep the copies in sync.
"""
from __future__ import annotations

import contextvars
import importlib.util
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from contextlib import nullcontext
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
_sentry_on = False
_tracing_on = False
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
    if _sentry_on and (tenant_id or user_id):
        import sentry_sdk

        if tenant_id:
            sentry_sdk.set_tag("tenant_id", str(tenant_id))
        if user_id:
            sentry_sdk.set_user({"id": str(user_id)})
    if _tracing_on and (tenant_id or user_id):
        from opentelemetry import trace

        span = trace.get_current_span()  # the request's span: lets you search traces by gym
        if span.is_recording():
            if tenant_id:
                span.set_attribute("tenant.id", str(tenant_id))
            if user_id:
                span.set_attribute("enduser.id", str(user_id))


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
        if _sentry_on:
            # On Sentry's per-request scope: an unhandled error is reported after this
            # middleware has already reset its context variables.
            import sentry_sdk

            sentry_sdk.set_tag("request_id", request_id)
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
    if _tracing_on:
        from opentelemetry import trace

        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            trace_id = format(span.trace_id, "032x")
            out["trace_id"] = trace_id
            out["logging.googleapis.com/trace"] = f"projects/{_PROJECT}/traces/{trace_id}" if _PROJECT else trace_id
            out["logging.googleapis.com/spanId"] = format(span.span_id, "016x")
            out["logging.googleapis.com/trace_sampled"] = span.trace_flags.sampled
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


# ---------------------------------------------------------------- error tracking (Sentry)
def _scrub_event(event: dict, _hint: dict) -> dict:
    for key in ("request_id", "tenant_id"):
        value = (_request_id if key == "request_id" else _tenant_id).get()
        if value:
            event.setdefault("tags", {})[key] = value
    if _user_id.get():
        event["user"] = {"id": _user_id.get()}  # id only: no email, name or IP
    for exc in (event.get("exception") or {}).get("values") or []:
        if isinstance(exc.get("value"), str):
            exc["value"] = redact(exc["value"])
        # Source lines shown around each frame can hold hardcoded secrets too.
        for frame in (exc.get("stacktrace") or {}).get("frames") or []:
            for key in ("context_line", "pre_context", "post_context"):
                if key in frame:
                    frame[key] = redact_values(frame[key])
    for key in ("message", "logentry"):
        if key in event:
            event[key] = redact_values(event[key])
    crumbs = event.get("breadcrumbs") or {}
    for crumb in crumbs.get("values") or []:
        if "message" in crumb:
            crumb["message"] = redact_values(crumb["message"])
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("cookies", None)
        request.pop("data", None)
        request["headers"] = {k: v for k, v in (request.get("headers") or {}).items()
                              if k.lower() in ("user-agent", "content-type", "x-request-id")}
        if "query_string" in request:
            request["query_string"] = redact_values(request["query_string"])
        if "url" in request:
            request["url"] = redact(request["url"])
    return event


def init_error_tracking(service: str) -> bool:
    """Send errors to Sentry when SENTRY_DSN is set. Call once at startup (after configure_logging)."""
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return False
    global _sentry_on
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT") or os.getenv("ENVIRONMENT") or "production",
        release=f"{service}@{_VERSION}",
        server_name=os.getenv("K_SERVICE") or service,
        send_default_pii=False,
        # Errors only by default (free plan quota); tracing is Phase 3.
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        before_send=_scrub_event,
        max_breadcrumbs=50,
        # Frame locals hold raw requests (headers, cookies, bodies): never send them.
        include_local_variables=False,
    )
    # A 5xx access line repeats the exception Sentry already has.
    ignore_logger("access")
    _sentry_on = True
    logging.getLogger(__name__).info("error tracking on (Sentry), release %s@%s", service, _VERSION)
    return True


# ---------------------------------------------------------------- tracing (OpenTelemetry)
def _instrument(library: str, module: str, cls: str, **kwargs) -> None:
    """Turn on one auto-instrumentation; a library this service doesn't use is skipped."""
    if importlib.util.find_spec(library) is None:
        return
    try:
        instrumentor = getattr(__import__(module, fromlist=[cls]), cls)()
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument(**kwargs)
    except ImportError:
        pass


def _trace_sql() -> None:
    """Query spans for every engine. Hooked on first connect rather than on create_engine,
    because our modules import create_engine before tracing starts (and tenant engines come later)."""
    import weakref

    if importlib.util.find_spec("sqlalchemy") is None:
        return
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer
    except ImportError:
        return
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    tracer = trace.get_tracer("sqlalchemy")
    usage = metrics.get_meter("sqlalchemy").create_up_down_counter("db.client.connections.usage")
    traced = weakref.WeakSet()

    @event.listens_for(Engine, "engine_connect")
    def _trace_engine(conn, *_):
        if conn.engine not in traced:
            traced.add(conn.engine)
            EngineTracer(tracer, conn.engine, usage)


def init_tracing(service: str, app: Any = None) -> bool:
    """Trace a share of requests when TRACE_SAMPLE_RATE > 0 (e.g. 0.1). Call at startup."""
    rate = float(os.getenv("TRACE_SAMPLE_RATE") or 0)
    if rate <= 0:
        return False
    try:
        return _setup_tracing(service, app, rate)
    except ImportError:
        # Tracing must never stop a service from starting: run without it.
        logging.getLogger(__name__).exception("tracing requested (TRACE_SAMPLE_RATE) but its packages are missing")
        return False


def _setup_tracing(service: str, app: Any, rate: float) -> bool:
    global _tracing_on
    if _tracing_on:  # already set up in this process (e.g. worker + API): just add the app
        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready,live,favicon.ico")
        return True
    from opentelemetry import propagate, trace
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    propagators = [TraceContextTextMapPropagator()]
    if (os.getenv("TRACE_EXPORTER") or ("gcp" if os.getenv("K_SERVICE") else "otlp")) == "gcp":
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.propagators.cloud_trace_propagator import CloudTraceFormatPropagator

        exporter = CloudTraceSpanExporter(project_id=_PROJECT or None)
        # Continue the trace Cloud Run starts (X-Cloud-Trace-Context): logs and spans share it.
        propagators.insert(0, CloudTraceFormatPropagator())
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()  # OTEL_EXPORTER_OTLP_ENDPOINT, e.g. http://jaeger:4318

    provider = TracerProvider(
        resource=Resource.create({"service.name": os.getenv("K_SERVICE") or service, "service.version": _VERSION}),
        # Follow the caller's decision; sample `rate` of new traces.
        sampler=ParentBased(TraceIdRatioBased(rate)),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    propagate.set_global_textmap(CompositePropagator(propagators))

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready,live,favicon.ico")
    _trace_sql()
    _instrument("redis", "opentelemetry.instrumentation.redis", "RedisInstrumentor")
    _instrument("httpx", "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor")
    _instrument("requests", "opentelemetry.instrumentation.requests", "RequestsInstrumentor")
    _tracing_on = True
    logging.getLogger(__name__).info("tracing on, sample rate %s", rate)
    return True


def trace_carrier() -> dict:
    """The current trace as Pub/Sub message attributes (empty when tracing is off)."""
    if not _tracing_on:
        return {}
    from opentelemetry import propagate

    carrier: dict = {}
    propagate.inject(carrier)
    return carrier


def consumer_span(name: str, attributes: Optional[dict] = None, carrier: Optional[dict] = None):
    """Context manager for handling a message: a span continuing the publisher's trace."""
    if not _tracing_on:
        return nullcontext()
    from opentelemetry import propagate, trace

    return trace.get_tracer("pubsub").start_as_current_span(
        name,
        context=propagate.extract(dict(carrier or {})),
        kind=trace.SpanKind.CONSUMER,
        attributes={k: str(v) for k, v in (attributes or {}).items() if v is not None},
    )
