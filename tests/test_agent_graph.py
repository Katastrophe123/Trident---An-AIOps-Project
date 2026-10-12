"""The graph: evidence -> diagnosis -> postmortem, and every way it can go wrong.

The most valuable tests in this file are the five scenario tests at the bottom.
They feed the graph the evidence each chaos scenario actually produces — taken
from the real runs recorded in RUNBOOK.md — and assert the agent names the right
root cause. The db_exhaustion case is the one worth reading: the service at
fault reports zero errors while its victims report many, and an agent that
blames the loudest victim has failed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from services.agent.config import AgentSettings
from services.agent.graph import CONFIDENCE_FLOOR, build_graph, run_investigation
from services.agent.llm import Completion, LLMClient, LLMError, LLMProviderBase
from services.agent.stub_provider import StubProvider
from services.agent.tools import Toolbox

SERVICES = ("orders", "payments", "inventory")


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


class ScriptedProvider(LLMProviderBase):
    def __init__(self, replies: list[str | Exception]) -> None:
        self._replies = list(replies)
        self.calls = 0
        self.contexts: list[dict[str, Any]] = []

    @property
    def source(self) -> str:
        return "llm"

    async def complete(
        self, *, system: str, user: str, context: dict[str, Any] | None = None
    ) -> Completion:
        self.calls += 1
        self.contexts.append(dict(context or {}))
        reply = self._replies.pop(0) if self._replies else "{}"
        if isinstance(reply, Exception):
            raise reply
        return Completion(text=reply, model="scripted-1", source="llm")


# --------------------------------------------------------------------------- #
# A fake demo system, built from the readings each scenario really produces.
# --------------------------------------------------------------------------- #
def world(
    *,
    logs: dict[str, list[dict[str, Any]]] | None = None,
    metrics: dict[str, dict[str, Any]] | None = None,
    down: set[str] | None = None,
    unhealthy: set[str] | None = None,
) -> Any:
    """An httpx handler standing in for the three services."""
    logs = logs or {}
    metrics = metrics or {}
    down = down or set()
    unhealthy = unhealthy or set()

    def handler(request: httpx.Request) -> httpx.Response:
        service = str(request.url.host)
        if service in down:
            raise httpx.ConnectError("connection refused", request=request)

        path = request.url.path
        if path == "/_logs":
            return httpx.Response(200, json={"service": service, "records": logs.get(service, [])})
        if path == "/metrics":
            base = {
                "service": f"{service}-service",
                "requests": 100,
                "errors": 0,
                "error_rate": 0.0,
                "average_latency_ms": 10.0,
                "recent_average_latency_ms": 10.0,
                "recent_p95_latency_ms": 15.0,
                "memory_mb": 80.0,
                "uptime_seconds": 300.0,
                "downstream_errors": {},
            }
            base.update(metrics.get(service, {}))
            return httpx.Response(200, json=base)
        if path == "/health":
            status = "unhealthy" if service in unhealthy else "healthy"
            code = 503 if status == "unhealthy" else 200
            return httpx.Response(code, json={"status": status, "service": service})
        return httpx.Response(404, json={})

    return handler


class FakeContainer:
    def __init__(self, service: str, state: str = "running") -> None:
        self.name = f"aiops-incident-agent-{service}-1"
        self.status = state
        self.labels = {
            "com.docker.compose.service": service,
            "com.docker.compose.project": "aiops-incident-agent",
        }
        self.attrs = {"State": {"Status": state, "RestartCount": 0, "ExitCode": 0}}


class FakeDocker:
    def __init__(self, states: dict[str, str]) -> None:
        self._containers = [FakeContainer(s, st) for s, st in states.items()]
        outer = self

        class Containers:
            def list(self, *, all: bool = False, filters: Any = None) -> list[FakeContainer]:
                return outer._containers

        self.containers = Containers()

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


def build(
    handler: Any,
    *,
    provider: LLMProviderBase | None = None,
    container_states: dict[str, str] | None = None,
    settings: AgentSettings | None = None,
) -> tuple[Any, Toolbox, LLMClient]:
    """A compiled graph wired to the fake world."""
    settings = settings or make_settings()
    toolbox = Toolbox(
        settings,
        services={name: f"http://{name}" for name in SERVICES},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        docker_client=FakeDocker(container_states or dict.fromkeys(SERVICES, "running")),
    )
    llm = LLMClient(provider or StubProvider())
    graph = build_graph(toolbox=toolbox, llm=llm, settings=settings)
    return graph, toolbox, llm


async def investigate(
    handler: Any,
    *,
    service: str = "payments",
    trigger: str = "health check returned unhealthy",
    provider: LLMProviderBase | None = None,
    container_states: dict[str, str] | None = None,
    settings: AgentSettings | None = None,
) -> Any:
    graph, toolbox, _ = build(
        handler,
        provider=provider,
        container_states=container_states,
        settings=settings,
    )
    try:
        return await run_investigation(
            graph,
            incident_id="11111111-1111-1111-1111-111111111111",
            alert_service=service,
            trigger=trigger,
        )
    finally:
        await toolbox.aclose()


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
class TestGraphShape:
    async def test_all_three_nodes_run_in_order(self) -> None:
        state = await investigate(world())
        assert [step.node for step in state.steps] == [
            "gather_evidence",
            "diagnose",
            "postmortem",
        ]
        assert [step.sequence for step in state.steps] == [1, 2, 3]

    async def test_every_step_is_timed(self) -> None:
        state = await investigate(world())
        assert all(step.latency_ms >= 0 for step in state.steps)
        assert all(step.status == "ok" for step in state.steps)

    async def test_evidence_covers_every_service(self) -> None:
        state = await investigate(world())
        assert {entry.service for entry in state.evidence.metrics} == set(SERVICES)
        assert {entry.service for entry in state.evidence.logs} == set(SERVICES)

    async def test_a_postmortem_is_always_produced(self) -> None:
        state = await investigate(world())
        assert state.postmortem is not None
        assert "## Root cause" in state.postmortem.body

    async def test_no_remediation_is_attempted(self) -> None:
        """Phase 2 is diagnosis-only. Proposed actions are recorded, not run."""
        state = await investigate(
            world(), container_states={**dict.fromkeys(SERVICES, "running"), "payments": "exited"}
        )
        assert state.diagnosis is not None
        assert state.diagnosis.proposed_actions  # it does propose one
        assert state.verification is None  # but nothing was applied
        assert state.attempts == 0


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
class TestFailurePaths:
    async def test_an_llm_outage_escalates_instead_of_guessing(self) -> None:
        provider = ScriptedProvider([LLMError("provider unreachable")])
        state = await investigate(world(), provider=provider)

        assert state.diagnosis is None
        assert state.status == "escalated"
        assert "provider unreachable" in (state.error or "")
        # And the trace still shows what happened.
        diagnose_step = next(s for s in state.steps if s.node == "diagnose")
        assert diagnose_step.status == "error"

    async def test_a_postmortem_is_still_written_without_a_diagnosis(self) -> None:
        """An investigation that produces nothing readable is useless."""
        provider = ScriptedProvider([LLMError("down")])
        state = await investigate(world(), provider=provider)

        assert state.postmortem is not None
        assert "could not produce a diagnosis" in state.postmortem.body

    async def test_persistent_malformed_output_escalates(self) -> None:
        provider = ScriptedProvider(["nonsense", "more nonsense", "still nonsense"])
        state = await investigate(
            world(), provider=provider, settings=make_settings(llm_max_format_retries=2)
        )
        assert state.status == "escalated"
        assert state.diagnosis is None

    async def test_a_good_diagnosis_survives_a_failed_postmortem(self) -> None:
        """The report falling back must not discard the diagnosis."""
        good = json.dumps(
            {
                "root_cause": "payments is down",
                "affected_service": "payments",
                "confidence": 0.9,
                "proposed_fix": "restart it",
                "reasoning": "container exited",
                "scenario": "crashed_service",
                "proposed_actions": [],
            }
        )
        provider = ScriptedProvider([good, LLMError("died after diagnosis")])
        state = await investigate(world(), provider=provider)

        assert state.diagnosis is not None
        assert state.status == "diagnosed"
        assert state.postmortem is not None
        assert "generated locally" in state.postmortem.body

    async def test_unreachable_services_do_not_stop_the_investigation(self) -> None:
        state = await investigate(
            world(down={"payments"}),
            container_states={"orders": "running", "payments": "exited", "inventory": "running"},
        )
        assert state.diagnosis is not None
        gather = next(s for s in state.steps if s.node == "gather_evidence")
        assert gather.output is not None
        assert "payments" in gather.output["unreachable_services"]

    async def test_missing_docker_is_recorded_as_a_gap(self) -> None:
        settings = make_settings(docker_enabled=False)
        toolbox = Toolbox(
            settings,
            services={name: f"http://{name}" for name in SERVICES},
            client=httpx.AsyncClient(transport=httpx.MockTransport(world())),
            docker_client=None,
        )
        graph = build_graph(toolbox=toolbox, llm=LLMClient(StubProvider()), settings=settings)
        state = await run_investigation(
            graph,
            incident_id="22222222-2222-2222-2222-222222222222",
            alert_service="orders",
            trigger="t",
        )
        await toolbox.aclose()

        assert "get_container_status" in state.evidence.failures
        # The diagnosis is still attempted from partial evidence.
        assert state.diagnosis is not None


# --------------------------------------------------------------------------- #
# Confidence and the allowlist
# --------------------------------------------------------------------------- #
class TestConfidenceAndSafety:
    @staticmethod
    def diagnosis_json(**overrides: Any) -> str:
        payload = {
            "root_cause": "something happened",
            "affected_service": "payments",
            "confidence": 0.9,
            "proposed_fix": "do a thing",
            "reasoning": "because",
            "scenario": None,
            "proposed_actions": [],
        }
        payload.update(overrides)
        return json.dumps(payload)

    async def test_high_confidence_is_diagnosed(self) -> None:
        provider = ScriptedProvider([self.diagnosis_json(confidence=0.9), "{}"])
        state = await investigate(world(), provider=provider)
        assert state.status == "diagnosed"

    async def test_low_confidence_escalates(self) -> None:
        provider = ScriptedProvider([self.diagnosis_json(confidence=0.2), "{}"])
        state = await investigate(world(), provider=provider)

        assert state.status == "escalated"
        step = next(s for s in state.steps if s.node == "diagnose")
        assert step.output is not None
        assert step.output["needs_more_evidence"] is True

    async def test_the_floor_is_exclusive(self) -> None:
        """Exactly at the floor counts as confident enough."""
        provider = ScriptedProvider([self.diagnosis_json(confidence=CONFIDENCE_FLOOR), "{}"])
        state = await investigate(world(), provider=provider)
        assert state.status == "diagnosed"

    async def test_actions_naming_unknown_tools_are_dropped(self) -> None:
        """The enforcement point for "the LLM never runs arbitrary commands".
        A model asking for a shell gets nothing, and the attempt is recorded."""
        provider = ScriptedProvider(
            [
                self.diagnosis_json(
                    proposed_actions=[
                        {"tool": "restart_container", "target": "payments", "reason": "ok"},
                        {"tool": "exec_shell", "target": "rm -rf /", "reason": "trust me"},
                        {"tool": "DROP TABLE orders", "target": None, "reason": "cleanup"},
                    ]
                ),
                "{}",
            ]
        )
        state = await investigate(world(), provider=provider)

        assert state.diagnosis is not None
        assert [a.tool for a in state.diagnosis.proposed_actions] == ["restart_container"]

        step = next(s for s in state.steps if s.node == "diagnose")
        assert step.output is not None
        assert set(step.output["rejected_actions"]) == {"exec_shell", "DROP TABLE orders"}

    async def test_the_source_is_recorded(self) -> None:
        """So a stub answer is never mistaken for a model's."""
        stub_state = await investigate(world())
        assert stub_state.diagnosis_source == "stub"

        provider = ScriptedProvider([self.diagnosis_json(), "{}"])
        llm_state = await investigate(world(), provider=provider)
        assert llm_state.diagnosis_source == "llm"


# --------------------------------------------------------------------------- #
# The five scenarios, with the readings the real runs produced.
# --------------------------------------------------------------------------- #
class TestScenarioDiagnosis:
    """Evidence here mirrors the figures recorded in RUNBOOK.md."""

    async def test_crashed_service(self) -> None:
        state = await investigate(
            world(
                down={"payments"},
                logs={
                    "orders": [
                        {
                            "level": "ERROR",
                            "event": "order_failed",
                            "downstream": "payments",
                            "failure_kind": "unavailable",
                            "reason": "payments unavailable: [Errno -2] Name or service not known",
                        }
                    ]
                },
                metrics={"orders": {"errors": 39, "downstream_errors": {"payments": 39}}},
            ),
            service="orders",
            container_states={"orders": "running", "payments": "exited", "inventory": "running"},
        )

        assert state.diagnosis is not None
        assert state.diagnosis.scenario == "crashed_service"
        assert state.diagnosis.affected_service == "payments"
        assert state.diagnosis.confidence >= 0.9
        assert "restart_container" in [a.tool for a in state.diagnosis.proposed_actions]

    async def test_bad_config(self) -> None:
        state = await investigate(
            world(
                unhealthy={"payments"},
                logs={
                    "payments": [
                        {
                            "level": "ERROR",
                            "event": "database_healthcheck_failed",
                            "error": "DBAPIError: password authentication "
                            'failed for user "aiops_app"',
                        }
                    ]
                },
                metrics={"orders": {"errors": 24, "downstream_errors": {"payments": 24}}},
            ),
            service="payments",
        )

        assert state.diagnosis is not None
        assert state.diagnosis.scenario == "bad_config"
        assert state.diagnosis.affected_service == "payments"
        # Container is UP - that is what separates this from a crash.
        assert state.evidence.containers.not_running == []

    async def test_slow_dependency(self) -> None:
        state = await investigate(
            world(
                metrics={
                    "inventory": {"recent_average_latency_ms": 3020.9, "errors": 0},
                    "orders": {"recent_average_latency_ms": 3086.3, "errors": 0},
                    "payments": {"recent_average_latency_ms": 5.6, "errors": 0},
                },
            ),
            service="orders",
        )

        assert state.diagnosis is not None
        assert state.diagnosis.scenario == "slow_dependency"
        # orders is slower in absolute terms (3086 ms vs 3021 ms) because it
        # waits on inventory, but inventory is the cause. Naming orders would
        # be blaming the victim.
        assert state.diagnosis.affected_service == "inventory"

    async def test_memory_leak(self) -> None:
        state = await investigate(
            world(
                metrics={
                    "orders": {"memory_mb": 564.1, "errors": 0, "requests": 452},
                    "payments": {"memory_mb": 78.0},
                    "inventory": {"memory_mb": 78.5},
                },
            ),
            service="orders",
        )

        assert state.diagnosis is not None
        assert state.diagnosis.scenario == "memory_leak"
        assert state.diagnosis.affected_service == "orders"
        # Lowest confidence of the five: one reading cannot prove a trend.
        assert state.diagnosis.confidence < 0.8

    async def test_db_exhaustion_blames_the_culprit_not_the_victims(self) -> None:
        """The hardest case. payments holds every connection and reports zero
        errors; orders and inventory are the ones failing. An agent that blames
        the loudest victim has failed."""
        slot_error = (
            "remaining connection slots are reserved for roles with the SUPERUSER attribute"
        )
        state = await investigate(
            world(
                unhealthy={"orders", "payments", "inventory"},
                logs={
                    "orders": [
                        {
                            "level": "ERROR",
                            "event": "database_healthcheck_failed",
                            "error": slot_error,
                        }
                    ],
                    "inventory": [
                        {
                            "level": "ERROR",
                            "event": "database_healthcheck_failed",
                            "error": slot_error,
                        }
                    ],
                    "payments": [],
                },
                metrics={
                    "orders": {"errors": 25},
                    "inventory": {"errors": 25},
                    "payments": {"errors": 0, "held_db_connections": 22},
                },
            ),
            service="orders",
        )

        assert state.diagnosis is not None
        assert state.diagnosis.scenario == "db_exhaustion"
        assert state.diagnosis.affected_service == "payments"
        assert state.diagnosis.affected_service != "orders"

    async def test_a_healthy_system_is_not_diagnosed_as_broken(self) -> None:
        """The case the plan calls hardest: nothing is wrong. A steady trickle
        of 4xx is the designed baseline, not an incident."""
        state = await investigate(
            world(
                logs={
                    "orders": [
                        {"level": "WARNING", "event": "reservation_unknown_sku", "sku": "SKU-NOPE"},
                        {"level": "INFO", "event": "order_confirmed"},
                    ]
                },
                metrics=dict.fromkeys(SERVICES, {"errors": 0}),
            ),
            service="orders",
        )

        assert state.diagnosis is not None
        assert state.diagnosis.scenario is None
        assert state.diagnosis.confidence < CONFIDENCE_FLOOR
        assert state.status == "escalated"


class TestStubProviderIsLabelled:
    async def test_stub_postmortems_say_so(self) -> None:
        """Nobody should mistake a rule-based report for a model's."""
        state = await investigate(world())
        assert state.postmortem is not None
        assert "stub provider" in state.postmortem.body
        assert state.diagnosis_source == "stub"

    async def test_the_provider_receives_structured_evidence_not_just_a_string(
        self,
    ) -> None:
        """The stub reads `context`, so the graph must pass the evidence as
        data rather than expecting it to be parsed back out of the prompt."""
        provider = ScriptedProvider(["{}", "{}", "{}"])
        await investigate(world(), provider=provider)

        assert provider.contexts[0]["task"] == "diagnose"
        assert "metrics" in provider.contexts[0]["evidence"]
        assert "containers" in provider.contexts[0]["evidence"]


class TestTransitionIsNotACrash:
    """Regression guard for a wrong diagnosis seen on the live system."""

    async def test_a_recreating_container_yields_low_confidence(self) -> None:
        state = await investigate(
            world(down={"payments"}),
            service="payments",
            container_states={
                "orders": "running",
                "payments": "created",  # mid `--force-recreate`
                "inventory": "running",
            },
        )

        assert state.diagnosis is not None
        assert state.diagnosis.scenario != "crashed_service"
        assert state.diagnosis.confidence < CONFIDENCE_FLOOR
        assert "transition" in state.diagnosis.root_cause.lower()
        # And it proposes no action, rather than restarting a healthy deploy.
        assert state.diagnosis.proposed_actions == []

    async def test_a_genuine_crash_still_reads_as_a_crash(self) -> None:
        """The fix must not blunt the scenario it was protecting."""
        state = await investigate(
            world(down={"payments"}),
            service="payments",
            container_states={
                "orders": "running",
                "payments": "exited",
                "inventory": "running",
            },
        )

        assert state.diagnosis is not None
        assert state.diagnosis.scenario == "crashed_service"
        assert state.diagnosis.confidence >= 0.9
