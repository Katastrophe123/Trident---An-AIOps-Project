"""Fault injection: the three in-process scenarios and the control endpoints.

The two container-level scenarios (crashed service, bad config) cannot be tested
here - they stop and recreate containers. They are covered by ``test_tools.py``
at the "did it issue the right compose command" level, and verified for real in
the RUNBOOK walkthrough.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.common.app import ServiceContext, build_app
from services.common.chaos import LEAK_CHUNK_MB, ChaosState
from services.common.db import Database
from tests.conftest import make_settings


@pytest.fixture
async def chaos() -> Any:
    """A bare ChaosState, always reset so a leak never outlives its test."""
    state = ChaosState(service_name="test-service")
    try:
        yield state
    finally:
        await state.reset()


class TestSlowDependency:
    def test_starts_with_no_delay(self, chaos: ChaosState) -> None:
        assert chaos.latency_ms == 0
        assert chaos.snapshot()["healthy"] is True

    async def test_apply_latency_is_a_no_op_when_clear(self, chaos: ChaosState) -> None:
        started = time.perf_counter()
        await chaos.apply_latency()
        assert (time.perf_counter() - started) < 0.05

    async def test_apply_latency_actually_sleeps(self, chaos: ChaosState) -> None:
        chaos.set_latency(120)
        started = time.perf_counter()
        await chaos.apply_latency()
        assert (time.perf_counter() - started) >= 0.1

    def test_zero_clears_the_delay(self, chaos: ChaosState) -> None:
        chaos.set_latency(500)
        chaos.set_latency(0)
        assert chaos.latency_ms == 0
        assert chaos.snapshot()["healthy"] is True

    def test_negative_is_floored_at_zero(self, chaos: ChaosState) -> None:
        chaos.set_latency(-100)
        assert chaos.latency_ms == 0


class TestMemoryLeak:
    async def test_the_leak_grows(self, chaos: ChaosState) -> None:
        await chaos.start_memory_leak()
        assert chaos.memory_leak_active is True

        # The first chunk is appended before the task's first sleep.
        await asyncio.sleep(0.05)
        first = chaos.leaked_mb
        assert first >= LEAK_CHUNK_MB

        await asyncio.sleep(0.6)
        assert chaos.leaked_mb > first

    async def test_starting_twice_does_not_double_the_rate(self, chaos: ChaosState) -> None:
        await chaos.start_memory_leak()
        task = chaos._leak_task  # noqa: SLF001
        await chaos.start_memory_leak()
        assert chaos._leak_task is task  # noqa: SLF001

    async def test_stopping_frees_the_memory_by_default(self, chaos: ChaosState) -> None:
        await chaos.start_memory_leak()
        await asyncio.sleep(0.05)
        assert chaos.leaked_mb > 0

        await chaos.stop_memory_leak()
        assert chaos.memory_leak_active is False
        assert chaos.leaked_mb == 0

    async def test_stopping_without_freeing_keeps_the_memory(self, chaos: ChaosState) -> None:
        """What a real leak looks like: the leaking code stops, the RSS does not
        come back. It is why the runbook's fix is `docker compose restart`."""
        await chaos.start_memory_leak()
        await asyncio.sleep(0.05)
        leaked = chaos.leaked_mb

        await chaos.stop_memory_leak(free=False)
        assert chaos.memory_leak_active is False
        assert chaos.leaked_mb == leaked

    async def test_stopping_when_never_started_is_safe(self, chaos: ChaosState) -> None:
        await chaos.stop_memory_leak()
        assert chaos.memory_leak_active is False


class TestDatabaseExhaustion:
    async def test_connections_are_counted_and_released(
        self, chaos: ChaosState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fake asyncpg: the real one needs PostgreSQL, which the unit suite
        does not have. What is under test is the holding and releasing."""
        closed: list[int] = []

        class FakeConnection:
            def __init__(self, index: int) -> None:
                self.index = index

            async def close(self) -> None:
                closed.append(self.index)

        counter = iter(range(100))

        async def fake_connect(dsn: str, **_: Any) -> FakeConnection:
            return FakeConnection(next(counter))

        monkeypatch.setattr("asyncpg.connect", fake_connect)

        opened = await chaos.exhaust_connections(5, "postgresql://x")
        assert opened == 5
        assert chaos.held_connections == 5
        assert chaos.snapshot()["held_db_connections"] == 5
        assert chaos.snapshot()["healthy"] is False

        released = await chaos.release_connections()
        assert released == 5
        assert chaos.held_connections == 0

    async def test_a_refusal_stops_early_and_is_itself_the_symptom(
        self, chaos: ChaosState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}

        async def flaky_connect(dsn: str, **_: Any) -> object:
            calls["n"] += 1
            if calls["n"] > 2:
                raise OSError("FATAL: too many connections for role")
            return type("C", (), {"close": lambda self: None})()

        monkeypatch.setattr("asyncpg.connect", flaky_connect)

        opened = await chaos.exhaust_connections(10, "postgresql://x")
        assert opened == 2
        assert chaos.held_connections == 2

    async def test_releasing_nothing_is_safe(self, chaos: ChaosState) -> None:
        assert await chaos.release_connections() == 0

    async def test_release_survives_a_connection_that_fails_to_close(
        self, chaos: ChaosState
    ) -> None:
        class Stubborn:
            async def close(self) -> None:
                raise OSError("connection already dead")

        chaos._held_connections.append(Stubborn())  # noqa: SLF001
        assert await chaos.release_connections() == 1
        assert chaos.held_connections == 0


class TestReset:
    async def test_reset_clears_every_fault_at_once(
        self, chaos: ChaosState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_connect(dsn: str, **_: Any) -> object:
            return type("C", (), {"close": lambda self: None})()

        monkeypatch.setattr("asyncpg.connect", fake_connect)

        chaos.set_latency(2000)
        await chaos.start_memory_leak()
        await chaos.exhaust_connections(3, "postgresql://x")
        await asyncio.sleep(0.05)
        assert chaos.snapshot()["healthy"] is False

        await chaos.reset()

        snapshot = chaos.snapshot()
        assert snapshot == {
            "service": "test-service",
            "latency_ms": 0,
            "memory_leak_active": False,
            "leaked_mb": 0,
            "held_db_connections": 0,
            "healthy": True,
        }


class TestChaosEndpoints:
    """``/_chaos`` is what both ``tools/chaos.py`` and the console drive."""

    async def test_state_is_reported(self, payments_client: AsyncClient) -> None:
        body = (await payments_client.get("/_chaos")).json()
        assert body["healthy"] is True
        assert body["latency_ms"] == 0

    async def test_latency_can_be_set_and_cleared(self, payments_client: AsyncClient) -> None:
        body = (await payments_client.post("/_chaos/latency?milliseconds=50")).json()
        assert body["latency_ms"] == 50
        assert body["healthy"] is False

        body = (await payments_client.post("/_chaos/latency?milliseconds=0")).json()
        assert body["latency_ms"] == 0
        assert body["healthy"] is True

    async def test_injected_latency_shows_up_in_the_measured_latency(
        self, payments_app: tuple[FastAPI, ServiceContext], payments_client: AsyncClient
    ) -> None:
        """The whole point of the scenario: the metric has to move."""
        await payments_client.get("/payments/count")
        await payments_client.post("/_chaos/latency?milliseconds=150")
        await payments_client.get("/payments/count")

        _, context = payments_app
        assert context.metrics.recent_average_latency_ms >= 75  # one fast, one slow
        await payments_client.post("/_chaos/latency?milliseconds=0")

    async def test_latency_above_the_cap_is_rejected(self, payments_client: AsyncClient) -> None:
        response = await payments_client.post("/_chaos/latency?milliseconds=120000")
        assert response.status_code == 422

    async def test_memory_leak_can_be_started_and_stopped(
        self, payments_client: AsyncClient
    ) -> None:
        body = (await payments_client.post("/_chaos/memory-leak?enabled=true")).json()
        assert body["memory_leak_active"] is True

        body = (await payments_client.post("/_chaos/memory-leak?enabled=false")).json()
        assert body["memory_leak_active"] is False
        assert body["leaked_mb"] == 0

    async def test_an_active_fault_makes_health_degraded_not_unhealthy(
        self, payments_client: AsyncClient
    ) -> None:
        """Degraded, because the service is still serving correctly - just
        slowly. A 503 here would tell the agent the wrong thing."""
        await payments_client.post("/_chaos/latency?milliseconds=10")

        response = await payments_client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["detail"] == "fault injection is active in this process"

        await payments_client.post("/_chaos/latency?milliseconds=0")
        assert (await payments_client.get("/health")).json()["status"] == "healthy"

    async def test_db_exhaust_releases_with_zero(self, payments_client: AsyncClient) -> None:
        body = (await payments_client.post("/_chaos/db-exhaust?connections=0")).json()
        assert body["held_db_connections"] == 0

    async def test_reset_endpoint_can_also_clear_metrics_and_logs(
        self, payments_app: tuple[FastAPI, ServiceContext], payments_client: AsyncClient
    ) -> None:
        await payments_client.get("/payments/count")
        _, context = payments_app
        assert context.metrics.requests >= 1

        await payments_client.post("/_chaos/reset?reset_metrics=true")
        assert context.metrics.requests == 0
        assert context.logs.tail(limit=50) == []

    async def test_reset_leaves_metrics_alone_by_default(
        self, payments_app: tuple[FastAPI, ServiceContext], payments_client: AsyncClient
    ) -> None:
        await payments_client.get("/payments/count")
        await payments_client.post("/_chaos/reset")

        _, context = payments_app
        assert context.metrics.requests >= 1


class TestChaosCanBeDisabled:
    """``CHAOS_ENABLED=false`` is what a deployed build would set."""

    @pytest.fixture
    async def locked_client(self, database: Database) -> Any:
        from services.payments.main import register_routes

        app, context = build_app(
            settings=make_settings("payments-service", chaos_enabled=False),
            title="payments-service",
            description="",
            database=database,
        )
        register_routes(app, context)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://payments"
        ) as client:
            yield client

    @pytest.mark.parametrize(
        "path",
        [
            "/_chaos/latency?milliseconds=100",
            "/_chaos/memory-leak?enabled=true",
            "/_chaos/db-exhaust?connections=5",
            "/_chaos/reset",
        ],
    )
    async def test_injection_is_forbidden(self, locked_client: AsyncClient, path: str) -> None:
        response = await locked_client.post(path)
        assert response.status_code == 403
        assert "disabled" in response.json()["error"]["message"]

    async def test_reading_the_state_is_still_allowed(self, locked_client: AsyncClient) -> None:
        """Observing is not injecting, so the console still renders."""
        assert (await locked_client.get("/_chaos")).status_code == 200
