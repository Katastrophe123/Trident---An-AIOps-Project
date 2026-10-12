"""console: a small live dashboard over the demo system.

Not in the project plan — added so the chaos scenarios are *visible*, which is
what Phase 1 is for. Design notes:

* **The browser talks to one origin.** The console fans out to the three
  services server-side, so there is no CORS setup and the static files are
  plain HTML/CSS/JS with no build step.
* **It is read-mostly.** It can inject the three *in-process* faults (slow
  dependency, memory leak, DB exhaustion) because those are just HTTP calls to
  a service's own ``/_chaos`` endpoints. It deliberately cannot stop or
  reconfigure containers: that needs the Docker socket, and the plan reserves
  socket access for the agent service. For those two scenarios the UI shows the
  exact command to run instead.
* **A down service is data, not an error.** Every fan-out call catches its own
  failure and reports ``unreachable``, because "payments is not answering" is
  precisely what the dashboard exists to show.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

from services.common.config import get_settings
from services.common.logging_setup import configure_logging, get_logger

logger = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: The services the console watches, in display order.
WATCHED: tuple[tuple[str, str], ...] = (
    ("orders", "orders_url"),
    ("payments", "payments_url"),
    ("inventory", "inventory_url"),
)

#: Container-level scenarios the console shows commands for rather than running.
MANUAL_SCENARIOS: dict[str, dict[str, str]] = {
    "crash": {
        "title": "Crashed service",
        "inject": "docker compose stop payments",
        "fix": "docker compose start payments",
        "why": "Needs the Docker socket, which is reserved for the agent service.",
    },
    "bad-config": {
        "title": "Bad config",
        "inject": "python -m tools.chaos bad-config --service payments",
        "fix": "python -m tools.chaos reset",
        "why": "Recreates the container with a wrong DB password.",
    },
}


class ServiceSnapshot(BaseModel):
    """Everything the dashboard shows for one service."""

    name: str
    url: str
    reachable: bool
    #: healthy | degraded | unhealthy | unreachable
    status: str
    detail: str | None = None
    uptime_seconds: float | None = None
    requests: int | None = None
    errors: int | None = None
    error_rate: float | None = None
    average_latency_ms: float | None = None
    recent_average_latency_ms: float | None = None
    recent_p95_latency_ms: float | None = None
    memory_mb: float | None = None
    dependencies: list[dict[str, Any]] = Field(default_factory=list)
    chaos: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Overview(BaseModel):
    """``GET /api/overview`` — one poll gives the whole dashboard."""

    services: list[ServiceSnapshot]
    orders_by_status: dict[str, int] = Field(default_factory=dict)
    #: True when every watched service reports healthy.
    all_healthy: bool
    manual_scenarios: dict[str, dict[str, str]] = Field(default_factory=dict)


class ConsoleClient:
    """Fans out to the demo services and normalises what comes back."""

    def __init__(self, targets: dict[str, str], *, timeout: float = 5.0) -> None:
        self._targets = targets
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def targets(self) -> dict[str, str]:
        return dict(self._targets)

    def base_url(self, service: str) -> str:
        try:
            return self._targets[service]
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Unknown service: {service}") from None

    async def _get_json(self, service: str, path: str) -> dict[str, Any] | None:
        """GET and parse, returning ``None`` on any failure."""
        try:
            response = await self._client.get(f"{self.base_url(service)}{path}")
            # 503 from /health still carries a usable body.
            if response.status_code >= 400 and response.status_code != 503:
                return None
            return dict(response.json())
        except (httpx.HTTPError, ValueError):
            return None

    async def snapshot(self, service: str) -> ServiceSnapshot:
        """Health + metrics + chaos state for one service."""
        url = self.base_url(service)
        health, metrics, chaos = await asyncio.gather(
            self._get_json(service, "/health"),
            self._get_json(service, "/metrics"),
            self._get_json(service, "/_chaos"),
        )

        if health is None:
            # The "crashed service" scenario lands here.
            return ServiceSnapshot(
                name=service,
                url=url,
                reachable=False,
                status="unreachable",
                error="no response from /health",
                chaos=chaos or {},
            )

        return ServiceSnapshot(
            name=service,
            url=url,
            reachable=True,
            status=str(health.get("status", "unknown")),
            detail=health.get("detail"),
            uptime_seconds=health.get("uptime_seconds"),
            dependencies=list(health.get("dependencies") or []),
            requests=(metrics or {}).get("requests"),
            errors=(metrics or {}).get("errors"),
            error_rate=(metrics or {}).get("error_rate"),
            average_latency_ms=(metrics or {}).get("average_latency_ms"),
            recent_average_latency_ms=(metrics or {}).get("recent_average_latency_ms"),
            recent_p95_latency_ms=(metrics or {}).get("recent_p95_latency_ms"),
            memory_mb=(metrics or {}).get("memory_mb"),
            chaos=chaos or {},
        )

    async def overview(self) -> Overview:
        snapshots = await asyncio.gather(*(self.snapshot(name) for name in self._targets))
        orders_summary = await self._get_json("orders", "/orders/summary") or {}
        return Overview(
            services=list(snapshots),
            orders_by_status={str(k): int(v) for k, v in orders_summary.items()},
            all_healthy=all(item.status == "healthy" for item in snapshots),
            manual_scenarios=MANUAL_SCENARIOS,
        )

    async def logs(
        self, service: str | None, *, limit: int, level: str | None
    ) -> list[dict[str, Any]]:
        """Merge recent logs, newest first."""
        names = [service] if service else list(self._targets)
        query = f"?limit={limit}" + (f"&level={level}" if level else "")
        results = await asyncio.gather(*(self._get_json(name, f"/_logs{query}") for name in names))
        records: list[dict[str, Any]] = []
        for payload in results:
            if payload:
                records.extend(payload.get("records") or [])
        records.sort(key=lambda item: item.get("epoch", 0), reverse=True)
        return records[:limit]

    async def post(self, service: str, path: str) -> dict[str, Any]:
        """Proxy a chaos control call, surfacing failures as 502."""
        try:
            response = await self._client.post(f"{self.base_url(service)}{path}")
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"{service} is not reachable: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text[:300])
        return dict(response.json())

    async def aclose(self) -> None:
        await self._client.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(service_name=settings.service_name or "console", level=settings.log_level)

    targets = {
        "orders": settings.orders_url,
        "payments": settings.payments_url,
        "inventory": settings.inventory_url,
    }
    client = ConsoleClient(targets)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("console_started", extra={"targets": targets})
        try:
            yield
        finally:
            await client.aclose()
            logger.info("console_stopped")

    app = FastAPI(
        title="aiops-console",
        description="Live dashboard over the AIOps demo system.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.client = client

    @app.get("/health", tags=["operations"], summary="Console's own health")
    async def health() -> dict[str, Any]:
        """Deliberately shallow: the console is up even when everything it
        watches is down, and saying otherwise would be misleading."""
        return {"status": "healthy", "service": "console", "watching": list(targets)}

    @app.get("/api/overview", response_model=Overview, tags=["console"])
    async def overview() -> Overview:
        """One poll returns health, metrics and fault state for every service."""
        return await client.overview()

    @app.get("/api/logs", tags=["console"])
    async def logs(
        service: str | None = Query(default=None, description="Omit to merge all"),
        limit: int = Query(default=80, ge=1, le=500),
        level: str | None = Query(default=None, description="Minimum level"),
    ) -> dict[str, Any]:
        records = await client.logs(service, limit=limit, level=level)
        return {"count": len(records), "records": records}

    @app.post("/api/chaos/latency", tags=["chaos"])
    async def set_latency(
        service: str = Query(default="inventory"),
        milliseconds: int = Query(default=3000, ge=0, le=60_000),
    ) -> dict[str, Any]:
        """Slow dependency. 0 clears it."""
        return await client.post(service, f"/_chaos/latency?milliseconds={milliseconds}")

    @app.post("/api/chaos/memory-leak", tags=["chaos"])
    async def memory_leak(
        service: str = Query(default="orders"), enabled: bool = Query(default=True)
    ) -> dict[str, Any]:
        """Memory leak, started or stopped."""
        return await client.post(service, f"/_chaos/memory-leak?enabled={str(enabled).lower()}")

    @app.post("/api/chaos/db-exhaust", tags=["chaos"])
    async def db_exhaust(
        service: str = Query(default="payments"),
        connections: int = Query(default=20, ge=0, le=200),
    ) -> dict[str, Any]:
        """Database exhaustion. 0 releases the held connections."""
        return await client.post(service, f"/_chaos/db-exhaust?connections={connections}")

    @app.post("/api/chaos/reset", tags=["chaos"])
    async def reset(
        reset_metrics: bool = Query(default=False),
    ) -> dict[str, Any]:
        """Clear in-process faults on every reachable service.

        Container-level scenarios are not covered - see ``manual_scenarios``.
        """
        outcomes: dict[str, Any] = {}
        for name in targets:
            try:
                outcomes[name] = await client.post(
                    name, f"/_chaos/reset?reset_metrics={str(reset_metrics).lower()}"
                )
            except HTTPException as exc:
                outcomes[name] = {"error": exc.detail}
        logger.info("console_reset", extra={"reset_metrics": reset_metrics})
        return {"reset": outcomes}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
