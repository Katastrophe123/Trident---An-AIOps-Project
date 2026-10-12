"""agent-service: the incident-response agent.

A separate FastAPI service, as the plan specifies, running the LangGraph
workflow. Phase 2 is diagnosis-only: it watches, investigates and writes a
report, and has no path to changing anything.

It reuses the shared application factory, so the agent exposes the same
``/health``, ``/metrics`` and ``/_logs`` surface as the services it watches.
That is not symmetry for its own sake - the agent is a service in the same
system, and when it misbehaves it should be diagnosable by the same means.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Query
from starlette.exceptions import HTTPException

from services.agent.config import AgentSettings, get_agent_settings
from services.agent.graph import build_graph, run_investigation
from services.agent.llm import LLMClient, build_llm_client
from services.agent.repository import IncidentRepository, step_payloads
from services.agent.schemas import (
    IncidentCreateRequest,
    IncidentSummaryView,
    PostmortemView,
    ToolView,
)
from services.agent.tools import TOOL_REGISTRY, Toolbox
from services.common.app import ServiceContext, build_app
from services.common.logging_setup import get_logger

logger = get_logger(__name__)

#: A service is only considered healthy for monitoring purposes if /health says
#: so. "degraded" is deliberately NOT an alert: the slow-dependency and
#: memory-leak scenarios report degraded while still serving, and alerting on
#: it would open an incident for every injected fault the moment it starts,
#: before there is any symptom to diagnose.
ALERTING_STATUSES = frozenset({"unhealthy", "unreachable"})


class AgentRuntime:
    """Holds the long-lived pieces: toolbox, LLM, compiled graph, repository."""

    def __init__(
        self,
        settings: AgentSettings,
        context: ServiceContext,
        *,
        toolbox: Toolbox | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        # Both are injectable so the API tests can run the whole service
        # against a fake demo system and a scripted model, with no network.
        self.settings = settings
        self.context = context
        self.toolbox = toolbox or Toolbox(settings)
        self.llm = llm or build_llm_client(settings)
        self.graph = build_graph(toolbox=self.toolbox, llm=self.llm, settings=settings)
        self.repository = IncidentRepository(context.database)
        #: One investigation at a time. Concurrent runs against one small demo
        #: system would contend for the same evidence and produce two reports
        #: for one fault.
        self._lock = asyncio.Lock()

    async def investigate(
        self,
        *,
        service: str,
        trigger: str,
        source: str = "manual",
        alert_detail: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Open an incident, run the graph, persist the result.

        Persistence is best-effort on both ends, which is not defensive
        programming for its own sake. The agent shares a database with the
        services it watches, so the database-exhaustion scenario takes the
        agent's own storage with it. Reaching a diagnosis and logging it beats
        refusing to investigate because the filing cabinet is locked.
        """
        try:
            incident_id = await self.repository.open_incident(
                service=service, trigger=trigger, source=source
            )
            persisted = True
        except Exception as exc:  # noqa: BLE001 - the investigation matters more
            incident_id = uuid.uuid4()
            persisted = False
            logger.error(
                "incident_not_persisted",
                extra={
                    "incident_id": str(incident_id),
                    "service": service,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "note": "investigating anyway; the diagnosis will only be in the logs",
                },
            )

        async with self._lock:
            try:
                state = await run_investigation(
                    self.graph,
                    incident_id=str(incident_id),
                    alert_service=service,
                    trigger=trigger,
                    alert_detail=alert_detail,
                )
            except Exception as exc:  # noqa: BLE001 - must still record the failure
                logger.exception("investigation_failed", extra={"incident_id": str(incident_id)})
                if persisted:
                    with contextlib.suppress(Exception):
                        await self.repository.mark_failed(
                            incident_id, f"{type(exc).__name__}: {exc}"
                        )
                return incident_id

            # Always log the conclusion, so a diagnosis reached while the
            # database was unavailable is still recoverable from the logs.
            logger.info(
                "investigation_complete",
                extra={
                    "incident_id": str(incident_id),
                    "status": state.status,
                    "root_cause": (state.diagnosis.root_cause if state.diagnosis else None),
                    "root_cause_service": (
                        state.diagnosis.affected_service if state.diagnosis else None
                    ),
                    "confidence": state.confidence,
                    "diagnosis_source": state.diagnosis_source,
                    "persisted": persisted,
                },
            )
            if persisted:
                try:
                    await self.repository.save_result(state)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "incident_result_not_persisted",
                        extra={
                            "incident_id": str(incident_id),
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        },
                    )
        return incident_id

    async def aclose(self) -> None:
        await self.toolbox.aclose()
        await self.llm.aclose()


async def monitor_once(runtime: AgentRuntime, failures: dict[str, int]) -> list[str]:
    """One polling pass over every watched service.

    Split out from the loop so it can be tested by calling it, rather than by
    starting a task and sleeping long enough to hope it ran. The first version
    of these tests raced the wall clock and failed under coverage, which is the
    usual tell that the seam is in the wrong place.

    ``failures`` is the caller's consecutive-failure counter, mutated in place.
    Returns the services an incident was opened for.

    Two guards matter here:

    * **A failure threshold**, so one dropped request during a restart does not
      open an incident.
    * **Deduplication against live incidents**, so a service that stays down
      produces one investigation rather than one per poll. That only works
      because recovery resolves incidents - without it the first incident for a
      service would silence the monitor for that service forever.
    """
    opened: list[str] = []

    for service in runtime.toolbox.services:
        health = await runtime.toolbox.get_health(service)
        status = "unreachable" if health is None else str(health.get("status"))

        if status not in ALERTING_STATUSES:
            failures.pop(service, None)
            await runtime.repository.resolve_incidents_for(service)
            continue

        failures[service] = failures.get(service, 0) + 1
        if failures[service] < runtime.settings.monitor_failure_threshold:
            continue

        if await runtime.repository.unresolved_incident_for(service) is not None:
            continue

        logger.warning(
            "monitor_alert",
            extra={
                "service": service,
                "status": status,
                "consecutive_failures": failures[service],
            },
        )
        await runtime.investigate(
            service=service,
            trigger=f"health check returned {status}",
            source="monitor",
            alert_detail=health,
        )
        opened.append(service)

    return opened


async def monitor_loop(runtime: AgentRuntime) -> None:
    """Run :func:`monitor_once` forever. The plan's "alert intake" node."""
    settings = runtime.settings
    failures: dict[str, int] = {}

    logger.info(
        "monitor_started",
        extra={
            "interval_seconds": settings.monitor_interval_seconds,
            "failure_threshold": settings.monitor_failure_threshold,
            "services": runtime.toolbox.services,
        },
    )

    while True:
        await asyncio.sleep(settings.monitor_interval_seconds)
        try:
            await monitor_once(runtime, failures)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the monitor must outlive one bad poll
            logger.exception("monitor_iteration_failed")


def register_routes(app: FastAPI, runtime: AgentRuntime) -> None:
    repository = runtime.repository
    settings = runtime.settings

    # Registered here rather than in create_app so the test harness builds a
    # service identical to production. A health check that only exists in one
    # of the two is a health check nothing verifies.
    async def llm_check() -> dict[str, Any]:
        """Surfaced so a stub-powered agent is never mistaken for one that is
        actually calling a model."""
        return {
            "name": "llm",
            "status": "healthy" if settings.llm_is_live else "degraded",
            "error": None
            if settings.llm_is_live
            else "no LLM_API_KEY; diagnoses come from the rule-based stub provider",
        }

    runtime.context.extra_health_checks.append(llm_check)

    @app.get("/tools", response_model=list[ToolView], tags=["agent"])
    async def list_tools() -> list[ToolView]:
        """The complete tool allowlist.

        Served so the restriction is inspectable rather than a claim in a
        README: anything absent here cannot be called, and ``enabled_in_graph``
        shows which tools the current phase can actually reach.
        """
        return [
            ToolView(
                name=spec.name,
                risk=spec.risk,  # type: ignore[arg-type]
                description=spec.description,
                enabled_in_graph=spec.enabled_in_graph,
            )
            for spec in TOOL_REGISTRY
        ]

    @app.post("/incidents", status_code=202, tags=["incidents"])
    async def create_incident(
        payload: IncidentCreateRequest, background: BackgroundTasks
    ) -> dict[str, Any]:
        """Trigger an investigation by hand, for testing."""
        if payload.wait:
            incident_id = await runtime.investigate(
                service=payload.service, trigger=payload.trigger, source="manual"
            )
            detail = await _detail_or_404(incident_id)
            return {"incident_id": str(incident_id), "incident": detail}

        incident_id = await repository.open_incident(
            service=payload.service, trigger=payload.trigger, source="manual"
        )

        async def run_later() -> None:
            state_id = incident_id
            try:
                state = await run_investigation(
                    runtime.graph,
                    incident_id=str(state_id),
                    alert_service=payload.service,
                    trigger=payload.trigger,
                )
                await repository.save_result(state)
            except Exception as exc:  # noqa: BLE001
                await repository.mark_failed(state_id, f"{type(exc).__name__}: {exc}")

        background.add_task(run_later)
        return {"incident_id": str(incident_id), "status": "open"}

    @app.get("/incidents", response_model=list[IncidentSummaryView], tags=["incidents"])
    async def list_incidents(
        limit: int = Query(default=50, ge=1, le=200),
        status: str | None = Query(default=None),
        service: str | None = Query(default=None),
    ) -> list[IncidentSummaryView]:
        incidents = await repository.list_incidents(limit=limit, status=status, service=service)
        return [
            IncidentSummaryView.model_validate(incident, from_attributes=True)
            for incident in incidents
        ]

    async def _detail_or_404(incident_id: uuid.UUID) -> dict[str, Any]:
        found = await repository.get_detail(incident_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"Unknown incident {incident_id}")
        incident, steps, actions, postmortem = found

        view = IncidentSummaryView.model_validate(incident, from_attributes=True)
        return {
            **view.model_dump(mode="json"),
            "proposed_fix": incident.proposed_fix,
            "error": incident.error,
            "attempts": incident.attempts,
            "steps": step_payloads(steps),
            "actions": [
                {
                    "tool": action.tool,
                    "target": action.target,
                    "risk": action.risk,
                    "status": action.status,
                    "approved_by": action.approved_by,
                    "result": action.result,
                }
                for action in actions
            ],
            "postmortem": (
                PostmortemView.model_validate(postmortem, from_attributes=True).model_dump(
                    mode="json"
                )
                if postmortem
                else None
            ),
        }

    @app.get("/incidents/{incident_id}", tags=["incidents"])
    async def get_incident(incident_id: uuid.UUID) -> dict[str, Any]:
        """Full trace: every node, its evidence and its decision."""
        return await _detail_or_404(incident_id)

    @app.get(
        "/incidents/{incident_id}/postmortem",
        response_model=PostmortemView,
        tags=["incidents"],
    )
    async def get_postmortem(incident_id: uuid.UUID) -> PostmortemView:
        postmortem = await repository.get_postmortem(incident_id)
        if postmortem is None:
            raise HTTPException(status_code=404, detail=f"No postmortem for incident {incident_id}")
        return PostmortemView.model_validate(postmortem, from_attributes=True)

    # Phase 3 will add POST /incidents/{id}/approve and /reject here, behind
    # the LangGraph interrupt. They are absent rather than stubbed: an endpoint
    # that accepts an approval and does nothing with it is worse than no
    # endpoint, because it implies a gate that does not exist.


def create_app() -> FastAPI:
    settings = get_agent_settings()

    app, context = build_app(
        settings=settings,
        title="agent-service",
        description=(
            "Incident-response agent. Phase 2: watches the demo system, "
            "investigates failures and writes a postmortem. Diagnosis only - "
            "no remediation tool is reachable from the graph."
        ),
    )

    runtime = AgentRuntime(settings, context)
    app.state.runtime = runtime
    register_routes(app, runtime)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        monitor: asyncio.Task[None] | None = None
        if settings.monitor_enabled:
            monitor = asyncio.create_task(monitor_loop(runtime))

        logger.info(
            "service_started",
            extra={
                "service": context.service_name,
                "llm_source": runtime.llm.source,
                "llm_live": settings.llm_is_live,
                "monitor_enabled": settings.monitor_enabled,
                "watching": runtime.toolbox.services,
            },
        )
        try:
            yield
        finally:
            if monitor is not None:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
            await runtime.aclose()
            await context.database.dispose()
            logger.info("service_stopped", extra={"service": context.service_name})

    app.router.lifespan_context = lifespan
    return app


app = create_app()
