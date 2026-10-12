"""Application factory: the shared skeleton every demo service is built from.

What it installs, in order:

1. **Request context middleware** — assigns a request id, injects any chaos
   latency, times the request, records metrics and logs one line per request.
2. **Error handlers** — one JSON envelope for every failure, carrying the
   request id so a log line can be tied to a response.
3. **Operational routes** — ``/health``, ``/metrics``, ``/_logs``, ``/_chaos``.

A service then adds its own business routes. Keeping this in one place is why
all three services report identical health and metrics shapes, which is what
makes the agent's tools uniform in Phase 2.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from services.common.chaos import ChaosState
from services.common.config import ServiceSettings
from services.common.db import Database
from services.common.logging_setup import (
    LogBuffer,
    configure_logging,
    get_logger,
    request_id_var,
)
from services.common.metrics import MetricsRegistry
from services.common.schemas import (
    ChaosStateResponse,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    LogsResponse,
    MetricsResponse,
)

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
SERVICE_VERSION = "0.1.0"

#: Routes excluded from metrics so polling the console does not drown out the
#: real traffic signal the agent reads.
UNMETERED_PATHS = frozenset({"/health", "/metrics", "/_logs", "/_chaos", "/openapi.json"})

#: Prefixes excluded as a whole. ``/_chaos`` needs this and an exact match is not
#: enough: without it ``POST /_chaos/reset?reset_metrics=true`` is itself recorded
#: *after* it clears the counters, so the metrics never actually read zero.
UNMETERED_PREFIXES = ("/_chaos/", "/docs", "/static/")


def is_metered(path: str) -> bool:
    """Whether a request to ``path`` counts as business traffic."""
    return path not in UNMETERED_PATHS and not path.startswith(UNMETERED_PREFIXES)


@dataclass
class ServiceContext:
    """Everything a service's route handlers need, built once at startup."""

    settings: ServiceSettings
    metrics: MetricsRegistry
    chaos: ChaosState
    logs: LogBuffer
    database: Database
    #: Set by a service if it wants /health to check more than the database.
    extra_health_checks: list[Callable[[], Awaitable[Any]]] = field(default_factory=list)

    @property
    def service_name(self) -> str:
        return self.settings.service_name


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Request id, chaos latency, timing, metrics and the access log line."""

    # ASGIApp, not FastAPI: that is what Starlette's middleware factory
    # protocol declares, and narrowing it here is what made add_middleware
    # fail to type-check.
    def __init__(self, app: ASGIApp, context: ServiceContext) -> None:
        super().__init__(app)
        self._context = context

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        request.state.request_id = request_id

        # The timer starts *before* the injected delay, deliberately: a slowed
        # service has to report its own latency as high. If the sleep sat
        # outside the timer, inventory would look fine while orders looked
        # broken, and the root cause would point at the wrong service.
        started = time.perf_counter()
        await self._context.chaos.apply_latency()

        try:
            response = await call_next(request)
        except Exception:
            latency_ms = (time.perf_counter() - started) * 1000
            self._record(request, 500, latency_ms)
            logger.exception(
                "request_errored",
                extra={"path": request.url.path, "latency_ms": round(latency_ms, 2)},
            )
            request_id_var.reset(token)
            raise

        latency_ms = (time.perf_counter() - started) * 1000
        self._record(request, response.status_code, latency_ms)
        response.headers[REQUEST_ID_HEADER] = request_id

        if is_metered(request.url.path):
            log = logger.error if response.status_code >= 500 else logger.info
            log(
                "request_completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "latency_ms": round(latency_ms, 2),
                },
            )
        request_id_var.reset(token)
        return response

    def _record(self, request: Request, status_code: int, latency_ms: float) -> None:
        if not is_metered(request.url.path):
            return
        # The route template, not the concrete path, so ids do not explode the keys.
        route = request.scope.get("route")
        endpoint = getattr(route, "path", None) or request.url.path
        self._context.metrics.record_request(
            endpoint=f"{request.method} {endpoint}",
            status_code=status_code,
            latency_ms=latency_ms,
        )


def _error_response(
    request: Request, status_code: int, code: str, message: str, **details: Any
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details or None),
        request_id=getattr(request.state, "request_id", None),
    )
    return JSONResponse(
        status_code=status_code, content=jsonable_encoder(payload, exclude_none=True)
    )


def register_error_handlers(app: FastAPI) -> None:
    """One response envelope for every failure."""

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            request,
            422,  # literal: the starlette constant was renamed in 0.47
            "validation_error",
            "Request payload failed validation",
            errors=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error_response(request, exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled_exception",
            extra={"path": request.url.path, "error_type": type(exc).__name__},
        )
        return _error_response(
            request,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred",
        )


def build_operational_router(context: ServiceContext) -> APIRouter:
    """``/health``, ``/metrics``, ``/_logs`` and the ``/_chaos`` controls."""
    router = APIRouter(tags=["operations"])

    @router.get(
        "/health",
        response_model=HealthResponse,
        summary="Liveness plus dependency health",
        responses={503: {"description": "A required dependency is unavailable"}},
    )
    async def health(response: Response) -> HealthResponse:
        """The endpoint the alert monitor polls every few seconds.

        Reports ``unhealthy`` (503) when the database is unreachable, and
        ``degraded`` (200) when the service can still serve but something is
        wrong - e.g. a leak is growing or latency is injected.
        """
        dependencies = []
        # Timed, so every dependency in this list reports latency the same way.
        # A rising postgres latency is the first symptom of pool contention.
        db_started = time.perf_counter()
        db_ok, db_error = await context.database.healthcheck()
        db_latency_ms = (time.perf_counter() - db_started) * 1000
        dependencies.append(
            {
                "name": "postgres",
                "status": "healthy" if db_ok else "unhealthy",
                "latency_ms": round(db_latency_ms, 2),
                "error": db_error,
            }
        )

        for check in context.extra_health_checks:
            dependencies.append(await check())

        chaos = context.chaos.snapshot()
        if not db_ok:
            overall = "unhealthy"
            detail = db_error
        elif any(item["status"] == "unhealthy" for item in dependencies):
            overall = "degraded"
            detail = "a dependency is unavailable"
        elif degraded_dependencies := [
            str(item["name"]) for item in dependencies if item["status"] == "degraded"
        ]:
            # A dependency running with reduced capability has to surface in the
            # overall status, or it is invisible to anything that reads only
            # that field. The agent uses this to report that it is running on
            # the rule-based stub rather than a real model.
            overall = "degraded"
            detail = f"reduced capability: {', '.join(degraded_dependencies)}"
        elif chaos["latency_ms"] > 0 or chaos["memory_leak_active"]:
            overall = "degraded"
            detail = "fault injection is active in this process"
        else:
            overall = "healthy"
            detail = None

        if overall == "unhealthy":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return HealthResponse(
            status=overall,  # type: ignore[arg-type]
            service=context.service_name,
            version=SERVICE_VERSION,
            uptime_seconds=round(context.metrics.uptime_seconds, 1),
            dependencies=dependencies,  # type: ignore[arg-type]
            detail=detail,
        )

    @router.get("/metrics", response_model=MetricsResponse, summary="Service metrics")
    async def metrics() -> MetricsResponse:
        """Request count, error count, average latency and memory."""
        return MetricsResponse.model_validate(context.metrics.snapshot())

    @router.get("/_logs", response_model=LogsResponse, summary="Recent log records")
    async def logs(
        limit: int = Query(default=100, ge=1, le=1000),
        level: str | None = Query(default=None, description="Minimum level, e.g. ERROR"),
        minutes: float | None = Query(
            default=None, ge=0, le=1440, description="Only records this recent"
        ),
    ) -> LogsResponse:
        """Newest-first tail of this process's log buffer.

        Phase 2's ``get_logs(service, minutes)`` tool reads exactly this.
        """
        records = context.logs.tail(
            limit=limit,
            level=level,
            since_seconds=minutes * 60 if minutes is not None else None,
        )
        return LogsResponse(
            service=context.service_name,
            count=len(records),
            records=records,  # type: ignore[arg-type]
        )

    return router


def build_chaos_router(context: ServiceContext) -> APIRouter:
    """In-process fault injection. Gated by ``CHAOS_ENABLED``."""
    router = APIRouter(prefix="/_chaos", tags=["chaos"])

    def _guard() -> None:
        if not context.settings.chaos_enabled:
            raise StarletteHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Chaos endpoints are disabled (set CHAOS_ENABLED=true)",
            )

    @router.get("", response_model=ChaosStateResponse, summary="Current fault state")
    async def state() -> ChaosStateResponse:
        return ChaosStateResponse.model_validate(context.chaos.snapshot())

    @router.post("/latency", response_model=ChaosStateResponse, summary="Slow dependency")
    async def set_latency(
        milliseconds: int = Query(ge=0, le=60_000, description="0 clears the delay"),
    ) -> ChaosStateResponse:
        """Add an artificial delay to every request this service handles."""
        _guard()
        context.chaos.set_latency(milliseconds)
        return ChaosStateResponse.model_validate(context.chaos.snapshot())

    @router.post("/memory-leak", response_model=ChaosStateResponse, summary="Memory leak")
    async def memory_leak(
        enabled: bool = Query(description="Start or stop the leak"),
    ) -> ChaosStateResponse:
        """Start a background task that keeps appending to a list.

        Stopping with ``enabled=false`` also frees the leaked memory, which a
        real leak would not - see the RUNBOOK.
        """
        _guard()
        if enabled:
            await context.chaos.start_memory_leak()
        else:
            await context.chaos.stop_memory_leak(free=True)
        return ChaosStateResponse.model_validate(context.chaos.snapshot())

    @router.post("/db-exhaust", response_model=ChaosStateResponse, summary="Database exhaustion")
    async def db_exhaust(
        connections: int = Query(default=20, ge=0, le=200, description="0 releases all"),
    ) -> ChaosStateResponse:
        """Open and hold raw connections until Postgres refuses more."""
        _guard()
        if connections == 0:
            await context.chaos.release_connections()
        else:
            await context.chaos.exhaust_connections(connections, context.database.asyncpg_dsn)
        return ChaosStateResponse.model_validate(context.chaos.snapshot())

    @router.post("/reset", response_model=ChaosStateResponse, summary="Clear all faults")
    async def reset(reset_metrics: bool = Query(default=False)) -> ChaosStateResponse:
        """Return this process to a healthy state."""
        _guard()
        await context.chaos.reset()
        if reset_metrics:
            context.metrics.reset()
            context.logs.clear()
        return ChaosStateResponse.model_validate(context.chaos.snapshot())

    return router


def build_app(
    *,
    settings: ServiceSettings,
    title: str,
    description: str,
    database: Database | None = None,
) -> tuple[FastAPI, ServiceContext]:
    """Create the app skeleton and its context.

    Returns both so the caller can register business routes and hang
    service-specific objects (clients, extra health checks) off the context.
    """
    logs = configure_logging(
        service_name=settings.service_name,
        level=settings.log_level,
        buffer_size=settings.log_buffer_size,
    )
    context = ServiceContext(
        settings=settings,
        metrics=MetricsRegistry(service_name=settings.service_name),
        chaos=ChaosState(service_name=settings.service_name),
        logs=logs,
        database=database or Database.from_settings(settings),
    )

    app = FastAPI(
        title=title,
        description=description,
        version=SERVICE_VERSION,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.context = context

    app.add_middleware(RequestContextMiddleware, context=context)
    register_error_handlers(app)
    app.include_router(build_operational_router(context))
    app.include_router(build_chaos_router(context))

    return app, context
