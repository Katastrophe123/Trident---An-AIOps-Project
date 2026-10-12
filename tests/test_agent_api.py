"""The agent's HTTP surface, persistence, and the alert monitor.

Runs the whole service — app factory, routes, graph, repository — against a
fake demo system and an in-memory database. No Docker, no network, no API key.

The trace endpoint gets the most attention, because it is what makes the agent
auditable. An agent that reaches a conclusion but cannot show which evidence
produced it is not something anyone should act on.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.agent.config import AgentSettings
from services.agent.llm import LLMClient
from services.agent.main import (
    ALERTING_STATUSES,
    AgentRuntime,
    monitor_loop,
    monitor_once,
    register_routes,
)
from services.agent.stub_provider import StubProvider
from services.agent.tools import Toolbox
from services.common.app import build_app
from services.common.db import Database
from services.common.models import Base
from tests.test_agent_graph import FakeDocker, ScriptedProvider, world

SERVICES = ("orders", "payments", "inventory")


def agent_settings(**overrides: Any) -> AgentSettings:
    values: dict[str, Any] = {
        "service_name": "agent-service",
        "environment": "test",
        "log_level": "INFO",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "docker_enabled": False,
        "monitor_enabled": False,
        "llm_provider": "stub",
    }
    values.update(overrides)
    return AgentSettings(**values)


@pytest.fixture
async def agent_db() -> AsyncIterator[Database]:
    """The agent's tables, in memory. Same metadata as the demo store."""
    db = Database("sqlite+aiosqlite:///:memory:")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


def build_agent(
    database: Database,
    *,
    handler: Any = None,
    provider: Any = None,
    container_states: dict[str, str] | None = None,
    settings: AgentSettings | None = None,
) -> tuple[FastAPI, AgentRuntime]:
    settings = settings or agent_settings()
    app, context = build_app(
        settings=settings, title="agent-service", description="", database=database
    )
    toolbox = Toolbox(
        settings,
        services={name: f"http://{name}" for name in SERVICES},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler or world())),
        docker_client=FakeDocker(container_states or dict.fromkeys(SERVICES, "running")),
    )
    runtime = AgentRuntime(
        settings, context, toolbox=toolbox, llm=LLMClient(provider or StubProvider())
    )
    app.state.runtime = runtime
    register_routes(app, runtime)
    return app, runtime


@pytest.fixture
async def agent(agent_db: Database) -> AsyncIterator[tuple[AsyncClient, AgentRuntime]]:
    app, runtime = build_agent(agent_db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://agent", timeout=30.0
    ) as client:
        yield client, runtime
    await runtime.aclose()


class TestOperationalSurface:
    async def test_the_agent_exposes_the_same_endpoints_it_consumes(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        """Symmetry with a purpose: when the agent misbehaves it should be
        diagnosable by the same means as everything else."""
        client, _ = agent
        for path in ("/health", "/metrics", "/_logs"):
            assert (await client.get(path)).status_code == 200, path

    async def test_health_admits_when_there_is_no_real_model(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        """Otherwise a stub-powered agent looks indistinguishable from one
        actually calling a model."""
        client, _ = agent
        body = (await client.get("/health")).json()

        llm = next(dep for dep in body["dependencies"] if dep["name"] == "llm")
        assert llm["status"] == "degraded"
        assert "stub" in (llm["error"] or "")

    async def test_health_is_healthy_when_a_key_is_configured(self, agent_db: Database) -> None:
        app, runtime = build_agent(
            agent_db,
            settings=agent_settings(llm_provider="openai", llm_api_key="sk-test"),
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://agent") as client:
            body = (await client.get("/health")).json()
        await runtime.aclose()

        llm = next(dep for dep in body["dependencies"] if dep["name"] == "llm")
        assert llm["status"] == "healthy"


class TestToolsEndpoint:
    async def test_the_allowlist_is_published(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        """Served so the restriction is inspectable rather than a claim in a
        README."""
        client, _ = agent
        tools = (await client.get("/tools")).json()

        assert {tool["name"] for tool in tools} == {
            "get_logs",
            "get_metrics",
            "get_container_status",
            "restart_container",
            "rollback_config",
        }

    async def test_risky_tools_are_marked_unreachable_in_this_phase(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        client, _ = agent
        tools = (await client.get("/tools")).json()

        for tool in tools:
            if tool["risk"] == "risky":
                assert tool["enabled_in_graph"] is False, tool["name"]


class TestManualTrigger:
    async def test_posting_an_incident_runs_the_whole_graph(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        client, _ = agent
        response = await client.post(
            "/incidents", json={"service": "payments", "trigger": "manual test"}
        )

        assert response.status_code == 202
        body = response.json()
        incident = body["incident"]
        assert uuid.UUID(body["incident_id"])
        assert [step["node"] for step in incident["steps"]] == [
            "gather_evidence",
            "diagnose",
            "postmortem",
        ]
        assert incident["postmortem"] is not None

    async def test_the_incident_records_the_diagnosis(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        client, _ = agent
        body = (await client.post("/incidents", json={"service": "orders", "trigger": "t"})).json()
        incident = body["incident"]

        assert incident["root_cause"]
        assert incident["diagnosis_source"] == "stub"
        assert incident["confidence"] is not None
        assert incident["status"] in {"diagnosed", "escalated"}

    async def test_background_mode_returns_immediately(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        client, _ = agent
        body = (
            await client.post(
                "/incidents", json={"service": "payments", "trigger": "t", "wait": False}
            )
        ).json()

        assert body["status"] == "open"
        assert uuid.UUID(body["incident_id"])

    @pytest.mark.parametrize(
        "payload",
        [
            {"service": "billing"},  # not a known service
            {"service": "orders", "trigger": ""},  # empty trigger
        ],
    )
    async def test_invalid_payloads_are_rejected(
        self, agent: tuple[AsyncClient, AgentRuntime], payload: dict[str, Any]
    ) -> None:
        client, _ = agent
        assert (await client.post("/incidents", json=payload)).status_code == 422


class TestTrace:
    """The audit trail. This is what makes a conclusion checkable."""

    async def test_every_node_records_what_it_found(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        client, _ = agent
        created = (
            await client.post("/incidents", json={"service": "orders", "trigger": "t"})
        ).json()
        detail = (await client.get(f"/incidents/{created['incident_id']}")).json()

        gather = next(s for s in detail["steps"] if s["node"] == "gather_evidence")
        assert set(gather["output"]["services_polled"]) == set(SERVICES)
        assert gather["latency_ms"] >= 0

        diagnose = next(s for s in detail["steps"] if s["node"] == "diagnose")
        assert "root_cause" in diagnose["output"]
        assert "confidence" in diagnose["output"]
        assert diagnose["output"]["confidence_floor"] == 0.4

    async def test_the_trace_shows_which_evidence_was_missing(self, agent_db: Database) -> None:
        """Diagnosing from partial evidence is fine; hiding the gaps is not."""
        app, runtime = build_agent(
            agent_db,
            handler=world(down={"payments"}),
            container_states={"orders": "running", "payments": "exited", "inventory": "running"},
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://agent", timeout=30.0
        ) as client:
            created = (
                await client.post("/incidents", json={"service": "orders", "trigger": "t"})
            ).json()
            detail = (await client.get(f"/incidents/{created['incident_id']}")).json()
        await runtime.aclose()

        gather = next(s for s in detail["steps"] if s["node"] == "gather_evidence")
        assert "payments" in gather["output"]["unreachable_services"]
        assert gather["output"]["containers_not_running"] == ["payments"]

    async def test_proposed_actions_are_recorded_as_proposed_not_applied(
        self, agent_db: Database
    ) -> None:
        """Phase 2 cannot act, and the stored row has to say so."""
        app, runtime = build_agent(
            agent_db,
            container_states={"orders": "running", "payments": "exited", "inventory": "running"},
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://agent", timeout=30.0
        ) as client:
            created = (
                await client.post("/incidents", json={"service": "orders", "trigger": "t"})
            ).json()
            detail = (await client.get(f"/incidents/{created['incident_id']}")).json()
        await runtime.aclose()

        assert detail["actions"]
        for action in detail["actions"]:
            assert action["status"] == "proposed"
            assert action["approved_by"] is None
            assert action["result"] is None
            assert action["risk"] == "risky"

    async def test_an_unknown_incident_is_a_404(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        client, _ = agent
        response = await client.get(f"/incidents/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_a_malformed_id_is_a_422(self, agent: tuple[AsyncClient, AgentRuntime]) -> None:
        client, _ = agent
        assert (await client.get("/incidents/not-a-uuid")).status_code == 422


class TestListAndPostmortem:
    async def test_incidents_are_listed_newest_first(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        client, _ = agent
        for service in ("orders", "payments"):
            await client.post("/incidents", json={"service": service, "trigger": "t"})

        incidents = (await client.get("/incidents")).json()
        assert len(incidents) == 2
        assert incidents[0]["created_at"] >= incidents[1]["created_at"]

    async def test_filtering_by_service(self, agent: tuple[AsyncClient, AgentRuntime]) -> None:
        client, _ = agent
        await client.post("/incidents", json={"service": "orders", "trigger": "t"})
        await client.post("/incidents", json={"service": "payments", "trigger": "t"})

        filtered = (await client.get("/incidents?service=payments")).json()
        assert [item["service"] for item in filtered] == ["payments"]

    async def test_the_postmortem_endpoint_returns_the_report(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        client, _ = agent
        created = (
            await client.post("/incidents", json={"service": "orders", "trigger": "t"})
        ).json()

        report = (await client.get(f"/incidents/{created['incident_id']}/postmortem")).json()
        assert "## Root cause" in report["body"]
        assert report["source"] == "stub"

    async def test_no_postmortem_yet_is_a_404(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        client, runtime = agent
        incident_id = await runtime.repository.open_incident(
            service="orders", trigger="not run yet"
        )
        response = await client.get(f"/incidents/{incident_id}/postmortem")
        assert response.status_code == 404

    async def test_approval_endpoints_do_not_exist_yet(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        """Absent rather than stubbed: an endpoint that accepts an approval and
        does nothing implies a gate that is not there."""
        client, _ = agent
        created = (
            await client.post("/incidents", json={"service": "orders", "trigger": "t"})
        ).json()
        response = await client.post(f"/incidents/{created['incident_id']}/approve")
        assert response.status_code == 404


class TestPersistence:
    async def test_a_failed_investigation_is_still_recorded(self, agent_db: Database) -> None:
        """An agent whose failure mode is "no record at all" cannot be debugged."""
        from services.agent.llm import LLMError

        app, runtime = build_agent(agent_db, provider=ScriptedProvider([LLMError("provider down")]))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://agent", timeout=30.0
        ) as client:
            created = (
                await client.post("/incidents", json={"service": "orders", "trigger": "t"})
            ).json()
            detail = (await client.get(f"/incidents/{created['incident_id']}")).json()
        await runtime.aclose()

        assert detail["status"] == "escalated"
        assert "provider down" in (detail["error"] or "")
        diagnose = next(s for s in detail["steps"] if s["node"] == "diagnose")
        assert diagnose["status"] == "error"
        # A report still exists, explaining that there is no diagnosis.
        assert detail["postmortem"] is not None

    async def test_an_oversized_evidence_blob_is_truncated_not_dropped(
        self, agent_db: Database
    ) -> None:
        """Step columns are bounded, so a huge blob must shrink rather than
        fail the whole write."""
        from services.agent.repository import MAX_BLOB, _dump

        payload = {"records": ["x" * 200 for _ in range(200)]}
        dumped = _dump(payload)

        assert dumped is not None
        assert len(dumped) < MAX_BLOB + 200
        import json

        assert json.loads(dumped)["truncated"] is True

    async def test_steps_are_stored_in_execution_order(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        client, _ = agent
        created = (
            await client.post("/incidents", json={"service": "orders", "trigger": "t"})
        ).json()
        detail = (await client.get(f"/incidents/{created['incident_id']}")).json()

        sequences = [step["sequence"] for step in detail["steps"]]
        assert sequences == sorted(sequences) == [1, 2, 3]


class TestAlertMonitor:
    async def test_degraded_does_not_raise_an_alert(self) -> None:
        """The slow-dependency and memory-leak scenarios report degraded while
        still serving. Alerting on that would open an incident the instant a
        fault is injected, before there is anything to diagnose."""
        assert "degraded" not in ALERTING_STATUSES
        assert "healthy" not in ALERTING_STATUSES
        assert {"unhealthy", "unreachable"} == ALERTING_STATUSES

    async def test_the_threshold_absorbs_a_single_bad_poll(self, agent_db: Database) -> None:
        """A restart drops a request or two; the threshold exists for that.

        Driven by calling the pass directly rather than starting the loop and
        sleeping: the first version of this raced the wall clock and failed
        under coverage.
        """
        app, runtime = build_agent(
            agent_db,
            handler=world(unhealthy={"payments"}),
            settings=agent_settings(monitor_failure_threshold=3),
        )
        failures: dict[str, int] = {}

        assert await monitor_once(runtime, failures) == []  # 1 of 3
        assert await monitor_once(runtime, failures) == []  # 2 of 3
        assert await monitor_once(runtime, failures) == ["payments"]  # 3 of 3

        await runtime.aclose()

    async def test_the_failure_counter_resets_on_recovery(self, agent_db: Database) -> None:
        """Two failures, a recovery, then one failure must not alert."""
        app, runtime = build_agent(
            agent_db,
            handler=world(unhealthy={"payments"}),
            settings=agent_settings(monitor_failure_threshold=3),
        )
        failures: dict[str, int] = {}
        await monitor_once(runtime, failures)
        await monitor_once(runtime, failures)
        assert failures["payments"] == 2

        # payments recovers.
        runtime.toolbox._client = httpx.AsyncClient(  # noqa: SLF001
            transport=httpx.MockTransport(world())
        )
        assert await monitor_once(runtime, failures) == []
        assert "payments" not in failures

        await runtime.aclose()

    async def test_a_live_incident_is_not_duplicated(self, agent_db: Database) -> None:
        """Without this, a two-minute outage at a five-second poll produces
        twenty-four identical investigations."""
        app, runtime = build_agent(
            agent_db,
            handler=world(unhealthy={"payments"}),
            settings=agent_settings(monitor_failure_threshold=1),
        )
        await runtime.repository.open_incident(
            service="payments", trigger="existing", source="monitor"
        )

        failures: dict[str, int] = {}
        for _ in range(5):
            assert await monitor_once(runtime, failures) == []

        await runtime.aclose()

    async def test_a_healthy_system_is_left_alone(self, agent_db: Database) -> None:
        app, runtime = build_agent(agent_db, settings=agent_settings())
        failures: dict[str, int] = {}

        for _ in range(3):
            assert await monitor_once(runtime, failures) == []
        assert failures == {}

        await runtime.aclose()

    async def test_one_outage_produces_exactly_one_incident(self, agent_db: Database) -> None:
        """The whole point of the dedup, stated as a count."""
        app, runtime = build_agent(
            agent_db,
            handler=world(unhealthy={"payments"}),
            settings=agent_settings(monitor_failure_threshold=1),
        )
        failures: dict[str, int] = {}
        for _ in range(6):
            await monitor_once(runtime, failures)

        incidents = await runtime.repository.list_incidents(service="payments")
        await runtime.aclose()

        assert len(incidents) == 1

    async def test_the_monitor_survives_a_failing_poll(self, agent_db: Database) -> None:
        """One exception must not kill the monitor for the rest of the run."""
        app, runtime = build_agent(
            agent_db,
            settings=agent_settings(monitor_interval_seconds=0.01),
        )
        calls = {"n": 0}

        async def flaky(service: str) -> dict[str, Any] | None:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient")
            return {"status": "healthy"}

        runtime.toolbox.get_health = flaky  # type: ignore[method-assign]

        import asyncio
        import contextlib

        task = asyncio.create_task(monitor_loop(runtime))
        await asyncio.sleep(0.08)
        assert not task.done()  # still running after the failures
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await runtime.aclose()

        assert calls["n"] >= 3


class TestConcurrency:
    async def test_investigations_do_not_overlap(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        """Two concurrent runs against one small system would contend for the
        same evidence and produce two reports for one fault."""
        import asyncio

        client, _ = agent
        results = await asyncio.gather(
            client.post("/incidents", json={"service": "orders", "trigger": "a"}),
            client.post("/incidents", json={"service": "payments", "trigger": "b"}),
        )

        assert all(response.status_code == 202 for response in results)
        incidents = (await client.get("/incidents")).json()
        assert len(incidents) == 2
        # Both completed rather than one being lost to the lock.
        assert all(item["status"] in {"diagnosed", "escalated"} for item in incidents)


class TestIncidentLifecycle:
    """Found on the live system: without resolution, dedup silences the monitor.

    The monitor skips a service that already has a live incident, so that one
    outage produces one investigation. But Phase 2 applies no fix and so never
    resolves anything - which meant the first incident for a service blocked
    every later one, forever. Recovery is the resolution signal.
    """

    async def test_recovery_resolves_a_live_incident(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        _, runtime = agent
        incident_id = await runtime.repository.open_incident(
            service="payments", trigger="went down", source="monitor"
        )

        resolved = await runtime.repository.resolve_incidents_for("payments")

        assert resolved == 1
        incident = await runtime.repository.get_incident(incident_id)
        assert incident is not None
        assert incident.status == "resolved"
        assert incident.resolved_at is not None

    async def test_a_resolved_incident_stops_blocking_new_ones(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        _, runtime = agent
        await runtime.repository.open_incident(
            service="payments", trigger="first outage", source="monitor"
        )
        assert await runtime.repository.unresolved_incident_for("payments") is not None

        await runtime.repository.resolve_incidents_for("payments")

        assert await runtime.repository.unresolved_incident_for("payments") is None

    async def test_resolving_keeps_the_diagnosis_and_report(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        """Closing an incident must not erase what was learned from it."""
        client, runtime = agent
        created = (
            await client.post("/incidents", json={"service": "orders", "trigger": "t"})
        ).json()

        await runtime.repository.resolve_incidents_for("orders")
        detail = (await client.get(f"/incidents/{created['incident_id']}")).json()

        assert detail["status"] == "resolved"
        assert detail["root_cause"]
        assert detail["postmortem"] is not None

    async def test_resolving_a_healthy_service_is_a_no_op(
        self, agent: tuple[AsyncClient, AgentRuntime]
    ) -> None:
        _, runtime = agent
        assert await runtime.repository.resolve_incidents_for("inventory") == 0

    async def test_the_monitor_closes_incidents_when_health_returns(
        self, agent_db: Database
    ) -> None:
        """End to end: a service recovers and the monitor notices."""
        app, runtime = build_agent(agent_db, settings=agent_settings())
        incident_id = await runtime.repository.open_incident(
            service="payments", trigger="earlier outage", source="monitor"
        )

        # The fake world reports everything healthy.
        await monitor_once(runtime, {})

        incident = await runtime.repository.get_incident(incident_id)
        await runtime.aclose()

        assert incident is not None
        assert incident.status == "resolved"
        assert incident.resolved_at is not None
