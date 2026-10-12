"""The agent's tools: the allowlist, and the evidence they return.

Two things are under test here, and the first matters more than the second:

1. **The allowlist is the security boundary.** If an unknown tool name can get
   through, the "the LLM never runs arbitrary commands" claim is false.
2. **A tool must report a failure as evidence, never raise.** "payments did not
   answer" is the single most valuable observation the agent can make, so it
   has to arrive as data rather than as an exception that kills the
   investigation.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from services.agent.config import AgentSettings
from services.agent.tools import (
    TOOL_REGISTRY,
    TOOLS_BY_NAME,
    RemediationTools,
    Toolbox,
    is_allowed,
    risk_of,
)

SERVICES = {
    "orders": "http://orders",
    "payments": "http://payments",
    "inventory": "http://inventory",
}


def make_settings(**overrides: Any) -> AgentSettings:
    values: dict[str, Any] = {
        "environment": "test",
        "log_level": "INFO",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "docker_enabled": False,
        "monitor_enabled": False,
        "llm_provider": "stub",
    }
    values.update(overrides)
    return AgentSettings(**values)


def toolbox_with(handler: Any, **overrides: Any) -> Toolbox:
    """A Toolbox whose HTTP calls hit a mock transport."""
    return Toolbox(
        make_settings(**overrides),
        services=dict(SERVICES),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        docker_client=None,
    )


class TestAllowlist:
    def test_exactly_the_five_tools_from_the_plan(self) -> None:
        assert set(TOOLS_BY_NAME) == {
            "get_logs",
            "get_metrics",
            "get_container_status",
            "restart_container",
            "rollback_config",
        }

    def test_reads_are_safe_and_writes_are_risky(self) -> None:
        assert risk_of("get_logs") == "safe"
        assert risk_of("get_metrics") == "safe"
        assert risk_of("get_container_status") == "safe"
        assert risk_of("restart_container") == "risky"
        assert risk_of("rollback_config") == "risky"

    @pytest.mark.parametrize(
        "name",
        [
            "rm -rf /",
            "exec",
            "shell",
            "get_logs; drop table orders",
            "GET_LOGS",
            "",
            "restart_container ",
        ],
    )
    def test_anything_not_in_the_registry_is_refused(self, name: str) -> None:
        """Including near-misses: casing and whitespace are not forgiven."""
        assert is_allowed(name) is False

    def test_an_unknown_tool_is_treated_as_risky_not_safe(self) -> None:
        """Fail closed. An unrecognised name must never be auto-approved."""
        assert risk_of("something_new") == "risky"

    def test_only_the_read_tools_are_reachable_from_the_phase_2_graph(self) -> None:
        enabled = {spec.name for spec in TOOL_REGISTRY if spec.enabled_in_graph}
        assert enabled == {"get_logs", "get_metrics", "get_container_status"}

    def test_every_risky_tool_is_excluded_from_the_graph(self) -> None:
        """Phase 2 is diagnosis-only; this is what enforces it."""
        for spec in TOOL_REGISTRY:
            if spec.risk == "risky":
                assert spec.enabled_in_graph is False, spec.name

    def test_remediation_tools_are_on_a_separate_class(self) -> None:
        """There is deliberately no path from a graph node to a mutating call."""
        assert not hasattr(Toolbox, "restart_container")
        assert not hasattr(Toolbox, "rollback_config")
        assert hasattr(RemediationTools, "restart_container")
        assert hasattr(RemediationTools, "rollback_config")


class TestGetLogs:
    async def test_returns_records_and_counts_levels(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "service": "payments",
                    "count": 3,
                    "records": [
                        {"level": "ERROR", "event": "database_healthcheck_failed"},
                        {"level": "WARNING", "event": "chaos_latency_set"},
                        {"level": "INFO", "event": "request_completed"},
                    ],
                },
            )

        box = toolbox_with(handler)
        evidence = await box.get_logs("payments")

        assert evidence.error_count == 1
        assert evidence.warning_count == 1
        assert len(evidence.records) == 3
        assert evidence.unreachable is False
        await box.aclose()

    async def test_an_unreachable_service_is_evidence_not_an_exception(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        box = toolbox_with(handler)
        evidence = await box.get_logs("payments")

        assert evidence.unreachable is True
        assert "ConnectError" in (evidence.error or "")
        assert evidence.records == []
        await box.aclose()

    async def test_a_500_is_also_reported_rather_than_raised(self) -> None:
        box = toolbox_with(lambda request: httpx.Response(500, text="boom"))
        evidence = await box.get_logs("orders")
        assert evidence.unreachable is True
        await box.aclose()

    async def test_unparseable_json_is_reported(self) -> None:
        box = toolbox_with(lambda request: httpx.Response(200, text="not json"))
        evidence = await box.get_logs("orders")
        assert evidence.unreachable is True
        await box.aclose()

    async def test_an_unknown_service_name_does_not_hit_the_network(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not have made a request")

        box = toolbox_with(handler)
        evidence = await box.get_logs("billing")
        assert "unknown service" in (evidence.error or "")
        assert evidence.unreachable is False
        await box.aclose()

    async def test_the_window_is_passed_through(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"records": []})

        box = toolbox_with(handler)
        await box.get_logs("orders", minutes=15.0, limit=7)
        assert seen["params"]["minutes"] == "15.0"
        assert seen["params"]["limit"] == "7"
        await box.aclose()


class TestGetMetrics:
    async def test_maps_every_reading_the_agent_reasons_about(self) -> None:
        payload = {
            "service": "orders-service",
            "requests": 100,
            "errors": 25,
            "error_rate": 0.25,
            "average_latency_ms": 30.0,
            "recent_average_latency_ms": 3000.0,
            "recent_p95_latency_ms": 3100.0,
            "memory_mb": 83.2,
            "uptime_seconds": 120.0,
            "downstream_errors": {"payments": 25},
        }
        box = toolbox_with(lambda request: httpx.Response(200, json=payload))
        evidence = await box.get_metrics("orders")

        assert evidence.errors == 25
        assert evidence.recent_average_latency_ms == 3000.0
        assert evidence.downstream_errors == {"payments": 25}
        assert evidence.memory_mb == 83.2
        await box.aclose()

    async def test_missing_fields_stay_none_rather_than_defaulting_to_zero(self) -> None:
        """Zero and "not reported" mean different things to a diagnosis."""
        box = toolbox_with(lambda request: httpx.Response(200, json={"requests": 5}))
        evidence = await box.get_metrics("orders")
        assert evidence.requests == 5
        assert evidence.errors is None
        assert evidence.memory_mb is None
        await box.aclose()

    async def test_unreachable_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        box = toolbox_with(handler)
        evidence = await box.get_metrics("inventory")
        assert evidence.unreachable is True
        await box.aclose()


class TestGetHealth:
    async def test_a_503_body_is_kept(self) -> None:
        """The unhealthy body carries the diagnosis; discarding it would throw
        away the most useful evidence in the bad-config scenario."""
        box = toolbox_with(
            lambda request: httpx.Response(
                503, json={"status": "unhealthy", "detail": "password authentication failed"}
            )
        )
        health = await box.get_health("payments")
        assert health is not None
        assert health["status"] == "unhealthy"
        await box.aclose()

    async def test_unreachable_is_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        box = toolbox_with(handler)
        assert await box.get_health("payments") is None
        await box.aclose()

    async def test_health_is_not_in_the_allowlist(self) -> None:
        """It is the monitor's input, never offered to the model."""
        assert is_allowed("get_health") is False


class FakeContainer:
    """Stands in for a docker-py container object."""

    def __init__(
        self,
        service: str,
        state: str = "running",
        *,
        health: str | None = None,
        restart_count: int = 0,
        exit_code: int | None = 0,
        project: str = "aiops-incident-agent",
    ) -> None:
        self.name = f"{project}-{service}-1"
        self.status = state
        self.labels = {
            "com.docker.compose.service": service,
            "com.docker.compose.project": project,
        }
        self.attrs: dict[str, Any] = {
            "State": {
                "Status": state,
                "RestartCount": restart_count,
                "ExitCode": exit_code,
                "StartedAt": "2026-10-12T09:00:00Z",
                **({"Health": {"Status": health}} if health else {}),
            }
        }


class FakeDocker:
    """Minimal docker client: records filters, returns canned containers."""

    def __init__(self, containers: list[FakeContainer], *, fail: bool = False) -> None:
        self._containers = containers
        self._fail = fail
        self.filters_seen: list[dict[str, Any]] = []
        self.closed = False
        outer = self

        class Containers:
            def list(self, *, all: bool = False, filters: Any = None) -> list[FakeContainer]:
                outer.filters_seen.append(dict(filters or {}))
                if outer._fail:
                    raise RuntimeError("docker daemon went away")
                return outer._containers

        self.containers = Containers()

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class TestGetContainerStatus:
    async def test_reports_state_for_every_container(self) -> None:
        docker = FakeDocker(
            [
                FakeContainer("orders", health="healthy"),
                FakeContainer("payments", health="healthy"),
                FakeContainer("inventory", health="healthy"),
            ]
        )
        box = Toolbox(make_settings(), services=dict(SERVICES), docker_client=docker)
        evidence = await box.get_container_status()

        assert evidence.available is True
        assert {c.service for c in evidence.containers} == set(SERVICES)
        assert evidence.not_running == []
        await box.aclose()

    async def test_an_exited_container_appears_in_not_running(self) -> None:
        """The crashed-service signature."""
        docker = FakeDocker(
            [
                FakeContainer("orders"),
                FakeContainer("payments", state="exited", exit_code=0),
                FakeContainer("inventory"),
            ]
        )
        box = Toolbox(make_settings(), services=dict(SERVICES), docker_client=docker)
        evidence = await box.get_container_status()

        assert evidence.not_running == ["payments"]
        payments = next(c for c in evidence.containers if c.service == "payments")
        assert payments.state == "exited"
        await box.aclose()

    async def test_containers_are_matched_by_compose_label_not_by_name(self) -> None:
        """Substring matching on names would make "orders" match anything
        prefixed with it; the label is exact."""
        docker = FakeDocker([FakeContainer("orders")])
        box = Toolbox(make_settings(), services=dict(SERVICES), docker_client=docker)
        await box.get_container_status()

        assert docker.filters_seen
        assert docker.filters_seen[0]["label"] == (
            "com.docker.compose.project=aiops-incident-agent"
        )
        await box.aclose()

    async def test_containers_outside_the_watched_services_are_not_alerted_on(
        self,
    ) -> None:
        """The traffic generator exits normally and the agent must not report
        itself as down."""
        docker = FakeDocker(
            [
                FakeContainer("orders"),
                FakeContainer("payments"),
                FakeContainer("inventory"),
                FakeContainer("traffic", state="exited"),
                FakeContainer("agent"),
            ]
        )
        box = Toolbox(make_settings(), services=dict(SERVICES), docker_client=docker)
        evidence = await box.get_container_status()

        assert evidence.not_running == []
        assert len(evidence.containers) == 5  # all reported, none alerted on
        await box.aclose()

    async def test_a_docker_failure_degrades_rather_than_raising(self) -> None:
        docker = FakeDocker([], fail=True)
        box = Toolbox(make_settings(), services=dict(SERVICES), docker_client=docker)
        evidence = await box.get_container_status()

        assert evidence.available is False
        assert "docker daemon went away" in (evidence.error or "")
        await box.aclose()

    async def test_docker_disabled_reports_unavailable_without_connecting(self) -> None:
        box = Toolbox(
            make_settings(docker_enabled=False), services=dict(SERVICES), docker_client=None
        )
        evidence = await box.get_container_status()

        assert evidence.available is False
        assert "unavailable" in (evidence.error or "")
        await box.aclose()

    async def test_the_agent_still_works_with_no_docker_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing socket is a degraded agent, not a dead one."""

        def boom() -> Any:
            raise RuntimeError("no socket at /var/run/docker.sock")

        monkeypatch.setattr("services.agent.tools.docker_from_env", boom)
        box = Toolbox(make_settings(docker_enabled=True), services=dict(SERVICES))

        evidence = await box.get_container_status()
        assert evidence.available is False
        # And the HTTP tools are unaffected.
        assert box.services == list(SERVICES)
        await box.aclose()


class TestRemediationToolsAreNotWired:
    """They exist, they are tested, and nothing in Phase 2 can reach them."""

    async def test_restart_container_targets_the_right_container(self) -> None:
        restarted: dict[str, Any] = {}

        class Restartable(FakeContainer):
            def restart(self, timeout: int = 10) -> None:
                restarted["name"] = self.name
                restarted["timeout"] = timeout

            def reload(self) -> None:
                self.attrs["State"]["Status"] = "running"

        docker = FakeDocker([Restartable("payments", state="exited")])
        tools = RemediationTools(make_settings(), docker_client=docker)

        result = await tools.restart_container("payments")

        assert restarted["name"] == "aiops-incident-agent-payments-1"
        assert result["target"] == "payments"
        assert result["state"] == "running"

    async def test_restart_filters_on_both_project_and_service(self) -> None:
        class Restartable(FakeContainer):
            def restart(self, timeout: int = 10) -> None:
                return None

            def reload(self) -> None:
                return None

        docker = FakeDocker([Restartable("payments")])
        tools = RemediationTools(make_settings(), docker_client=docker)
        await tools.restart_container("payments")

        label = docker.filters_seen[0]["label"]
        assert "com.docker.compose.project=aiops-incident-agent" in label
        assert "com.docker.compose.service=payments" in label

    async def test_restarting_an_unknown_service_raises_rather_than_guessing(
        self,
    ) -> None:
        docker = FakeDocker([])
        tools = RemediationTools(make_settings(), docker_client=docker)

        with pytest.raises(LookupError, match="no container for service"):
            await tools.restart_container("billing")


class TestTransitionalContainers:
    """A container being replaced is not a container that crashed.

    Found live: injecting bad-config recreates the container, and the monitor
    fired while it was mid-recreate. The agent diagnosed `crashed_service` at
    0.95 confidence - correct for that instant, and completely wrong about the
    actual fault. Separating the two states is what stops that.
    """

    async def test_exited_is_a_crash(self) -> None:
        docker = FakeDocker([FakeContainer("payments", state="exited")])
        box = Toolbox(make_settings(), services=dict(SERVICES), docker_client=docker)
        evidence = await box.get_container_status()

        assert evidence.not_running == ["payments"]
        assert evidence.transitioning == []
        await box.aclose()

    @pytest.mark.parametrize("state", ["created", "restarting", "paused", "removing"])
    async def test_transitional_states_are_not_crashes(self, state: str) -> None:
        docker = FakeDocker([FakeContainer("payments", state=state)])
        box = Toolbox(make_settings(), services=dict(SERVICES), docker_client=docker)
        evidence = await box.get_container_status()

        assert evidence.not_running == []
        assert evidence.transitioning == ["payments"]
        await box.aclose()

    async def test_an_unrecognised_state_is_treated_as_stopped(self) -> None:
        """Fail visible: an unknown state must not vanish from the evidence."""
        docker = FakeDocker([FakeContainer("payments", state="something-new")])
        box = Toolbox(make_settings(), services=dict(SERVICES), docker_client=docker)
        evidence = await box.get_container_status()

        assert evidence.not_running == ["payments"]
        await box.aclose()

    async def test_a_crash_outranks_a_transition(self) -> None:
        """If one container is genuinely down, that is the story - not the one
        next to it that happens to be restarting."""
        docker = FakeDocker(
            [
                FakeContainer("orders", state="restarting"),
                FakeContainer("payments", state="exited"),
                FakeContainer("inventory"),
            ]
        )
        box = Toolbox(make_settings(), services=dict(SERVICES), docker_client=docker)
        evidence = await box.get_container_status()

        assert evidence.not_running == ["payments"]
        assert evidence.transitioning == ["orders"]
        await box.aclose()
