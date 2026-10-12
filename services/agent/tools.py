"""The agent's tools — a fixed allowlist, not a shell.

This is the security design of the whole project, and it is deliberately
boring: there are five named tools, each a Python function with typed
arguments, and the LLM can neither add to the list nor pass anything through to
a shell. The model's output is *data* that gets validated against
:class:`~services.agent.schemas.Diagnosis`; a ``proposed_action`` naming a tool
that is not in this registry is dropped, with a warning, rather than attempted.

The alternative — give the model a shell and a prompt telling it to be careful
— fails the moment a log line contains something that reads like an
instruction. An allowlist cannot be talked out of its own contents.

Each tool carries a **risk** level, which is what Phase 3's approval gate will
branch on:

==========================  ======  ===================================
Tool                        Risk    Wired into the graph in Phase 2?
==========================  ======  ===================================
get_logs                    safe    yes
get_metrics                 safe    yes
get_container_status        safe    yes
restart_container           risky   no - needs the approval gate
rollback_config             risky   no - needs the approval gate
==========================  ======  ===================================

The two risky tools are implemented and tested here but are **not** reachable
from the graph: Phase 2 is diagnosis-only, per the plan's "get the diagnosis
right before the agent is allowed to act". Keeping them in the registry, marked
``enabled_in_graph=False``, means ``GET /tools`` can show the full surface and
Phase 3 only has to flip a flag and add the gate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from services.agent.config import AgentSettings
from services.agent.schemas import (
    ContainerEvidence,
    ContainerStatusEvidence,
    LogEvidence,
    MetricEvidence,
)
from services.common.logging_setup import get_logger

logger = get_logger(__name__)

#: Container states that mean "stopped, and staying stopped".
CRASHED_STATES = frozenset({"exited", "dead"})
#: States that mean "being started or replaced right now". A `--force-recreate`
#: passes through these, and an agent that reads them as a crash will diagnose
#: the transition instead of the fault underneath it.
TRANSITIONAL_STATES = frozenset({"created", "restarting", "paused", "removing"})


def docker_from_env() -> Any:
    """Construct a Docker client from the ambient environment.

    The import is isolated here for a mundane reason worth writing down: this
    repository has a ``docker/`` directory at its root (the entrypoint and the
    initdb SQL), and mypy treats it as a namespace package - so ``import
    docker`` resolves to *that* directory rather than the installed library,
    and every attribute then looks missing. ``ignore_missing_imports`` cannot
    help, because as far as mypy is concerned the module was found.

    Keeping it in one function means one suppression instead of one per call
    site. Nothing is lost in type safety: docker-py ships no ``py.typed``, so
    the client was untyped either way.
    """
    import docker  # type: ignore[import-not-found]

    return docker.from_env()  # type: ignore[attr-defined]


#: Services the agent can inspect, and how to reach them. Keys match the
#: compose service names so a metric, a log line and a container all line up.
ServiceMap = dict[str, str]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One allowlisted tool."""

    name: str
    risk: str  # safe | risky
    description: str
    enabled_in_graph: bool


TOOL_REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_logs",
        risk="safe",
        description="Recent structured log records for one service, newest first.",
        enabled_in_graph=True,
    ),
    ToolSpec(
        name="get_metrics",
        risk="safe",
        description="Request count, error count, latency and memory for one service.",
        enabled_in_graph=True,
    ),
    ToolSpec(
        name="get_container_status",
        risk="safe",
        description="Docker state of every container in the compose project.",
        enabled_in_graph=True,
    ),
    ToolSpec(
        name="restart_container",
        risk="risky",
        description="Restart one service's container. Requires approval (Phase 3).",
        enabled_in_graph=False,
    ),
    ToolSpec(
        name="rollback_config",
        risk="risky",
        description="Clear injected config overrides and recreate the service "
        "with its defaults. Requires approval (Phase 3).",
        enabled_in_graph=False,
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_REGISTRY}


def is_allowed(tool_name: str) -> bool:
    """Whether a name the model produced is a real tool."""
    return tool_name in TOOLS_BY_NAME


def risk_of(tool_name: str) -> str:
    """``safe`` or ``risky``; unknown names are treated as risky."""
    spec = TOOLS_BY_NAME.get(tool_name)
    return spec.risk if spec else "risky"


class Toolbox:
    """The agent's read-only view of the demo system.

    Holds one HTTP client for the lifetime of the service. Every method returns
    an evidence object and **never raises** on a failure to reach a service:
    "payments did not answer" is the single most valuable piece of evidence
    there is, so it has to arrive as data, not as an exception that aborts the
    investigation.
    """

    def __init__(
        self,
        settings: AgentSettings,
        *,
        services: ServiceMap | None = None,
        client: httpx.AsyncClient | None = None,
        docker_client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._services: ServiceMap = services or {
            "orders": settings.orders_url,
            "payments": settings.payments_url,
            "inventory": settings.inventory_url,
        }
        self._client = client or httpx.AsyncClient(timeout=settings.downstream_timeout_seconds)
        self._docker = docker_client
        self._docker_resolved = docker_client is not None

    @property
    def services(self) -> list[str]:
        return list(self._services)

    def base_url(self, service: str) -> str | None:
        return self._services.get(service)

    # --- safe tool: get_logs ---------------------------------------------
    async def get_logs(
        self, service: str, minutes: float | None = None, limit: int | None = None
    ) -> LogEvidence:
        """Recent log records for one service, newest first."""
        minutes = minutes if minutes is not None else self._settings.evidence_log_minutes
        limit = limit if limit is not None else self._settings.evidence_log_limit

        base = self.base_url(service)
        if base is None:
            return LogEvidence(
                service=service, minutes=minutes, error=f"unknown service {service!r}"
            )

        try:
            response = await self._client.get(
                f"{base}/_logs", params={"limit": limit, "minutes": minutes}
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The service being unreachable IS the finding.
            return LogEvidence(
                service=service,
                minutes=minutes,
                unreachable=True,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )

        records = list(payload.get("records") or [])
        return LogEvidence(
            service=service,
            minutes=minutes,
            records=records,
            error_count=sum(1 for r in records if r.get("level") == "ERROR"),
            warning_count=sum(1 for r in records if r.get("level") == "WARNING"),
        )

    # --- safe tool: get_metrics ------------------------------------------
    async def get_metrics(self, service: str) -> MetricEvidence:
        """Current metric readings for one service."""
        base = self.base_url(service)
        if base is None:
            return MetricEvidence(service=service, error=f"unknown service {service!r}")

        try:
            response = await self._client.get(f"{base}/metrics")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return MetricEvidence(
                service=service,
                unreachable=True,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )

        return MetricEvidence(
            service=service,
            requests=payload.get("requests"),
            errors=payload.get("errors"),
            error_rate=payload.get("error_rate"),
            average_latency_ms=payload.get("average_latency_ms"),
            recent_average_latency_ms=payload.get("recent_average_latency_ms"),
            recent_p95_latency_ms=payload.get("recent_p95_latency_ms"),
            memory_mb=payload.get("memory_mb"),
            uptime_seconds=payload.get("uptime_seconds"),
            downstream_errors=dict(payload.get("downstream_errors") or {}),
        )

    async def get_health(self, service: str) -> dict[str, Any] | None:
        """Raw ``/health`` body, or ``None`` if the service did not answer.

        Not in the tool allowlist: the alert monitor uses it to decide whether
        to open an incident, and it is never offered to the model.
        """
        base = self.base_url(service)
        if base is None:
            return None
        try:
            response = await self._client.get(f"{base}/health")
            # A 503 still carries the diagnosis in its body.
            if response.status_code >= 400 and response.status_code != 503:
                return None
            return dict(response.json())
        except (httpx.HTTPError, ValueError):
            return None

    # --- safe tool: get_container_status ---------------------------------
    def _resolve_docker(self) -> Any | None:
        """Connect to the Docker socket once, lazily.

        Lazy because the agent must still start and serve ``/health`` when the
        socket is missing - a container-status tool that is unavailable is a
        degraded agent, not a dead one.
        """
        if self._docker_resolved:
            return self._docker
        self._docker_resolved = True

        if not self._settings.docker_enabled:
            self._docker = None
            return None
        try:
            client = docker_from_env()
            client.ping()
        except Exception as exc:  # noqa: BLE001 - any failure means "no docker"
            logger.warning(
                "docker_unavailable",
                extra={"error": f"{type(exc).__name__}: {exc}"[:300]},
            )
            self._docker = None
            return None

        self._docker = client
        return client

    async def get_container_status(self) -> ContainerStatusEvidence:
        """Docker state of every container in the compose project.

        Runs the blocking Docker SDK in a worker thread so it cannot stall the
        event loop, and matches containers by the compose *label* rather than a
        name substring - names are prefixed and suffixed by compose, and
        substring matching would make "orders" match "orders-service" and any
        future "orders-worker" alike.
        """
        client = self._resolve_docker()
        if client is None:
            return ContainerStatusEvidence(
                available=False,
                error="Docker socket unavailable; container status not collected.",
            )

        def collect() -> list[ContainerEvidence]:
            containers = client.containers.list(
                all=True,
                filters={"label": f"com.docker.compose.project={self._settings.compose_project}"},
            )
            found: list[ContainerEvidence] = []
            for container in containers:
                labels = container.labels or {}
                state = (container.attrs or {}).get("State") or {}
                health = (state.get("Health") or {}).get("Status")
                found.append(
                    ContainerEvidence(
                        service=labels.get("com.docker.compose.service", container.name),
                        name=container.name,
                        state=str(state.get("Status") or container.status or "unknown"),
                        status=str(container.status or ""),
                        health=health,
                        restart_count=int(state.get("RestartCount") or 0),
                        exit_code=state.get("ExitCode"),
                        started_at=state.get("StartedAt"),
                    )
                )
            return found

        try:
            containers = await asyncio.to_thread(collect)
        except Exception as exc:  # noqa: BLE001 - report, never raise
            return ContainerStatusEvidence(
                available=False,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )

        # Only the three demo services matter here. The traffic generator has no
        # health check and the agent restarting itself would be absurd, so
        # neither belongs in "not_running".
        watched = set(self._services)
        relevant = [item for item in containers if item.service in watched]
        not_running = sorted(item.service for item in relevant if item.state in CRASHED_STATES)
        transitioning = sorted(
            item.service for item in relevant if item.state in TRANSITIONAL_STATES
        )
        # Anything neither running nor recognised is treated as stopped: an
        # unknown state should not silently disappear from the evidence.
        unknown = sorted(
            item.service
            for item in relevant
            if item.state != "running"
            and item.state not in CRASHED_STATES
            and item.state not in TRANSITIONAL_STATES
        )
        return ContainerStatusEvidence(
            containers=containers,
            not_running=sorted({*not_running, *unknown}),
            transitioning=transitioning,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._docker is not None:
            try:
                self._docker.close()
            except Exception:  # noqa: BLE001 - closing must not raise
                logger.warning("docker_close_failed")


# --------------------------------------------------------------------------- #
# Risky tools — implemented, deliberately NOT wired into the graph.
#
# They live in a separate class so there is no path from a graph node to a
# mutating call. Phase 3 will construct this behind the approval interrupt.
# --------------------------------------------------------------------------- #
class RemediationTools:
    """The two mutating tools. Unreachable from the Phase 2 graph."""

    def __init__(self, settings: AgentSettings, *, docker_client: Any | None = None) -> None:
        self._settings = settings
        self._docker = docker_client

    def _client(self) -> Any:
        if self._docker is None:
            self._docker = docker_from_env()
        return self._docker

    def _container_for(self, service: str) -> Any:
        client = self._client()
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"com.docker.compose.project={self._settings.compose_project}",
                    f"com.docker.compose.service={service}",
                ]
            },
        )
        if not containers:
            raise LookupError(f"no container for service {service!r}")
        return containers[0]

    async def restart_container(self, service: str, *, stop_timeout: int = 20) -> dict[str, Any]:
        """Restart one service's container. Risky: requires approval."""

        def do_restart() -> dict[str, Any]:
            container = self._container_for(service)
            container.restart(timeout=stop_timeout)
            container.reload()
            return {
                "tool": "restart_container",
                "target": service,
                "container": container.name,
                "state": (container.attrs.get("State") or {}).get("Status", "unknown"),
            }

        result = await asyncio.to_thread(do_restart)
        logger.warning(
            "remediation_applied", extra={"tool": "restart_container", "target": service}
        )
        return result

    async def rollback_config(self, service: str) -> dict[str, Any]:
        """Clear injected config overrides and recreate the service.

        Reuses ``tools/compose.py`` rather than the Docker SDK, for the same
        reason ``chaos.py`` does: recreating a container with different
        environment is a compose operation, and the runbook documents the
        compose command a human would run.
        """
        from tools import compose

        def do_rollback() -> dict[str, Any]:
            cleared = compose.clear_overrides()
            compose.run("up", "-d", "--force-recreate", "--no-deps", service)
            return {
                "tool": "rollback_config",
                "target": service,
                "overrides_cleared": cleared,
            }

        result = await asyncio.to_thread(do_rollback)
        logger.warning("remediation_applied", extra={"tool": "rollback_config", "target": service})
        return result


#: Signature every remediation tool shares, so Phase 3's gate can dispatch
#: without a chain of if-statements.
RemediationCallable = Callable[..., Awaitable[dict[str, Any]]]
