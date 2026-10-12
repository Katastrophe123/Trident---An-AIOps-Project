"""Metrics, structured logging and the log buffer.

These are the surfaces the Phase 2 agent reads, so their shape is a contract,
not an implementation detail.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from services.common.app import UNMETERED_PATHS, ServiceContext
from services.common.logging_setup import (
    JsonFormatter,
    LogBuffer,
    configure_logging,
    request_id_var,
)
from services.common.metrics import RECENT_WINDOW, EndpointStats, MetricsRegistry


class TestMetricsRegistry:
    def test_starts_empty(self) -> None:
        registry = MetricsRegistry(service_name="svc")
        assert registry.requests == 0
        assert registry.errors == 0
        assert registry.average_latency_ms == 0.0
        assert registry.error_rate == 0.0

    def test_records_requests_and_latency(self) -> None:
        registry = MetricsRegistry(service_name="svc")
        registry.record_request(endpoint="GET /a", status_code=200, latency_ms=10)
        registry.record_request(endpoint="GET /a", status_code=200, latency_ms=20)

        assert registry.requests == 2
        assert registry.average_latency_ms == 15.0
        assert registry.errors == 0

    def test_only_5xx_counts_as_an_error(self) -> None:
        """A 404 or 409 is a business outcome; counting it as an error would
        make every out-of-stock order look like an incident."""
        registry = MetricsRegistry(service_name="svc")
        for code in (200, 201, 400, 404, 409, 422):
            registry.record_request(endpoint="GET /a", status_code=code, latency_ms=1)
        assert registry.errors == 0

        registry.record_request(endpoint="GET /a", status_code=500, latency_ms=1)
        registry.record_request(endpoint="GET /a", status_code=502, latency_ms=1)
        assert registry.errors == 2

    def test_error_rate(self) -> None:
        registry = MetricsRegistry(service_name="svc")
        for _ in range(3):
            registry.record_request(endpoint="GET /a", status_code=200, latency_ms=1)
        registry.record_request(endpoint="GET /a", status_code=500, latency_ms=1)
        assert registry.error_rate == 0.25

    def test_status_counts_bucket_by_class(self) -> None:
        registry = MetricsRegistry(service_name="svc")
        registry.record_request(endpoint="GET /a", status_code=201, latency_ms=1)
        registry.record_request(endpoint="GET /a", status_code=404, latency_ms=1)
        registry.record_request(endpoint="GET /a", status_code=502, latency_ms=1)
        assert registry.snapshot()["status_counts"] == {"2xx": 1, "4xx": 1, "5xx": 1}

    def test_per_endpoint_breakdown(self) -> None:
        registry = MetricsRegistry(service_name="svc")
        registry.record_request(endpoint="POST /orders", status_code=201, latency_ms=10)
        registry.record_request(endpoint="POST /orders", status_code=500, latency_ms=30)
        registry.record_request(endpoint="GET /health", status_code=200, latency_ms=1)

        endpoints = registry.snapshot()["endpoints"]
        assert endpoints["POST /orders"]["requests"] == 2
        assert endpoints["POST /orders"]["errors"] == 1
        assert endpoints["POST /orders"]["average_latency_ms"] == 20.0

    def test_recent_average_moves_faster_than_the_lifetime_average(self) -> None:
        """The reason both exist: a latency spike has to be visible immediately,
        and a lifetime average over thousands of fast requests barely budges."""
        registry = MetricsRegistry(service_name="svc")
        for _ in range(1000):
            registry.record_request(endpoint="GET /a", status_code=200, latency_ms=5)
        for _ in range(RECENT_WINDOW):
            registry.record_request(endpoint="GET /a", status_code=200, latency_ms=3000)

        assert registry.recent_average_latency_ms == pytest.approx(3000, rel=0.01)
        assert registry.average_latency_ms < 600  # lifetime is still dominated by the fast ones

    def test_recent_window_is_bounded(self) -> None:
        registry = MetricsRegistry(service_name="svc")
        for _ in range(RECENT_WINDOW * 3):
            registry.record_request(endpoint="GET /a", status_code=200, latency_ms=1)
        assert len(registry._recent_latencies) == RECENT_WINDOW  # noqa: SLF001

    def test_p95_is_at_the_top_of_the_window(self) -> None:
        registry = MetricsRegistry(service_name="svc")
        for value in range(1, 101):
            registry.record_request(endpoint="GET /a", status_code=200, latency_ms=value)
        assert registry.recent_p95_latency_ms >= 95

    def test_downstream_errors_name_the_offender(self) -> None:
        registry = MetricsRegistry(service_name="orders")
        registry.record_downstream_error("payments")
        registry.record_downstream_error("payments")
        registry.record_downstream_error("inventory")
        assert registry.snapshot()["downstream_errors"] == {"payments": 2, "inventory": 1}

    def test_memory_is_a_positive_number_of_mib(self) -> None:
        assert MetricsRegistry.memory_mb() > 0

    def test_reset_zeroes_everything(self) -> None:
        registry = MetricsRegistry(service_name="svc")
        registry.record_request(endpoint="GET /a", status_code=500, latency_ms=10)
        registry.record_downstream_error("payments")
        registry.reset()

        snapshot = registry.snapshot()
        assert snapshot["requests"] == 0
        assert snapshot["errors"] == 0
        assert snapshot["endpoints"] == {}
        assert snapshot["downstream_errors"] == {}

    def test_snapshot_has_every_field_the_plan_asks_for(self) -> None:
        snapshot = MetricsRegistry(service_name="svc").snapshot()
        assert {"requests", "errors", "average_latency_ms", "memory_mb"} <= set(snapshot)

    def test_endpoint_stats_average_with_no_requests(self) -> None:
        assert EndpointStats().average_latency_ms == 0.0


class TestMetricsEndpoint:
    async def test_business_traffic_is_counted(
        self, orders_client: AsyncClient, order_payload: dict[str, Any]
    ) -> None:
        await orders_client.post("/orders", json=order_payload)
        metrics = (await orders_client.get("/metrics")).json()

        assert metrics["requests"] >= 1
        assert "POST /orders" in metrics["endpoints"]
        assert metrics["memory_mb"] > 0

    async def test_operational_routes_are_not_counted(self, orders_client: AsyncClient) -> None:
        """Otherwise the console polling every 2s would swamp the real signal."""
        for _ in range(5):
            await orders_client.get("/health")
            await orders_client.get("/metrics")

        metrics = (await orders_client.get("/metrics")).json()
        assert metrics["requests"] == 0
        assert "/health" in UNMETERED_PATHS

    async def test_an_unreachable_downstream_is_a_502_and_an_error(
        self, orders_stack: dict[str, Any], order_payload: dict[str, Any]
    ) -> None:
        """The crashed-service signature: a connect error, not a refusal.

        Swapped in at the transport layer because ASGITransport runs the app
        in-process, so there is no socket that can actually refuse a connection.
        """
        import httpx

        client: AsyncClient = orders_stack["client"]

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        orders_stack["downstreams"].inventory._client = httpx.AsyncClient(  # noqa: SLF001
            transport=httpx.MockTransport(refuse), base_url="http://inventory"
        )

        response = await client.post("/orders", json=order_payload)
        assert response.status_code == 502
        assert "inventory unavailable" in response.json()["error"]["message"]

        metrics = (await client.get("/metrics")).json()
        assert metrics["errors"] >= 1
        assert metrics["downstream_errors"].get("inventory", 0) >= 1

    async def test_the_failed_order_is_recorded_for_the_audit_trail(
        self, orders_stack: dict[str, Any], order_payload: dict[str, Any]
    ) -> None:
        """A failed order must still be persisted, or there is no evidence."""
        import httpx

        client: AsyncClient = orders_stack["client"]

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        orders_stack["downstreams"].payments._client = httpx.AsyncClient(  # noqa: SLF001
            transport=httpx.MockTransport(refuse), base_url="http://payments"
        )

        await client.post("/orders", json=order_payload)

        summary = (await client.get("/orders/summary")).json()
        assert summary.get("failed") == 1


class TestLogBuffer:
    def test_is_bounded(self) -> None:
        """The buffer must not become a second memory leak."""
        buffer = LogBuffer(maxlen=10)
        for index in range(50):
            buffer.append({"event": f"e{index}", "level": "INFO", "epoch": time.time()})
        assert len(buffer) == 10

    def test_tail_is_newest_first(self) -> None:
        buffer = LogBuffer(maxlen=10)
        for index in range(5):
            buffer.append({"event": f"e{index}", "level": "INFO", "epoch": time.time()})
        assert [item["event"] for item in buffer.tail(limit=2)] == ["e4", "e3"]

    def test_filters_by_minimum_level(self) -> None:
        buffer = LogBuffer(maxlen=10)
        now = time.time()
        buffer.append({"event": "info", "level": "INFO", "epoch": now})
        buffer.append({"event": "warn", "level": "WARNING", "epoch": now})
        buffer.append({"event": "err", "level": "ERROR", "epoch": now})

        assert {item["event"] for item in buffer.tail(level="WARNING")} == {"warn", "err"}
        assert {item["event"] for item in buffer.tail(level="ERROR")} == {"err"}

    def test_filters_by_age(self) -> None:
        buffer = LogBuffer(maxlen=10)
        buffer.append({"event": "old", "level": "INFO", "epoch": time.time() - 600})
        buffer.append({"event": "new", "level": "INFO", "epoch": time.time()})
        assert [item["event"] for item in buffer.tail(since_seconds=60)] == ["new"]

    def test_clear(self) -> None:
        buffer = LogBuffer(maxlen=5)
        buffer.append({"event": "x", "level": "INFO", "epoch": time.time()})
        buffer.clear()
        assert len(buffer) == 0


class TestJsonFormatter:
    def _record(self, **extra: Any) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="something_happened",
            args=(),
            exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_output_is_one_json_object(self) -> None:
        payload = json.loads(JsonFormatter("svc").format(self._record()))
        assert payload["event"] == "something_happened"
        assert payload["service"] == "svc"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "test.logger"
        assert "timestamp" in payload and "epoch" in payload

    def test_extra_fields_become_top_level_keys(self) -> None:
        payload = json.loads(
            JsonFormatter("svc").format(self._record(order_id="abc", latency_ms=12.5))
        )
        assert payload["order_id"] == "abc"
        assert payload["latency_ms"] == 12.5

    def test_request_id_is_attached_from_the_context(self) -> None:
        token = request_id_var.set("req-123")
        try:
            payload = json.loads(JsonFormatter("svc").format(self._record()))
        finally:
            request_id_var.reset(token)
        assert payload["request_id"] == "req-123"

    def test_unserialisable_values_do_not_break_logging(self) -> None:
        payload = json.loads(JsonFormatter("svc").format(self._record(thing=object())))
        assert "thing" in payload  # rendered via default=str


class TestConfigureLogging:
    def test_returns_a_buffer_that_receives_records(self) -> None:
        buffer = configure_logging(service_name="svc", level="INFO", buffer_size=50)
        logging.getLogger("test").info("hello", extra={"k": "v"})

        records = buffer.tail(limit=5)
        assert any(item["event"] == "hello" and item["k"] == "v" for item in records)

    def test_level_filtering_applies(self) -> None:
        buffer = configure_logging(service_name="svc", level="WARNING", buffer_size=50)
        logging.getLogger("test").info("suppressed")
        logging.getLogger("test").warning("kept")

        events = {item["event"] for item in buffer.tail(limit=10)}
        assert "suppressed" not in events
        assert "kept" in events

    def test_an_unknown_level_is_rejected(self) -> None:
        with pytest.raises(KeyError):
            configure_logging(service_name="svc", level="CHATTY")


class TestLogsEndpoint:
    async def test_returns_records_newest_first(self, inventory_client: AsyncClient) -> None:
        await inventory_client.post(
            "/reservations",
            json={"order_id": str(uuid.uuid4()), "sku": "SKU-LAPTOP-13", "quantity": 1},
        )
        body = (await inventory_client.get("/_logs?limit=20")).json()

        assert body["count"] >= 1
        events = [item["event"] for item in body["records"]]
        assert "stock_reserved" in events

    async def test_level_filter(self, inventory_client: AsyncClient) -> None:
        await inventory_client.post(
            "/reservations",
            json={"order_id": str(uuid.uuid4()), "sku": "SKU-NOPE", "quantity": 1},
        )
        body = (await inventory_client.get("/_logs?level=WARNING&limit=20")).json()
        assert all(item["level"] in {"WARNING", "ERROR", "CRITICAL"} for item in body["records"])

    async def test_request_id_ties_a_log_line_to_a_response(
        self, orders_client: AsyncClient, order_payload: dict[str, Any]
    ) -> None:
        """This is what makes a trace followable across services."""
        response = await orders_client.post(
            "/orders", json=order_payload, headers={"X-Request-ID": "trace-xyz"}
        )
        assert response.headers["X-Request-ID"] == "trace-xyz"

        body = (await orders_client.get("/_logs?limit=50")).json()
        assert any(item.get("request_id") == "trace-xyz" for item in body["records"])

    @pytest.mark.parametrize("query", ["limit=0", "limit=5000", "minutes=-1"])
    async def test_invalid_query_parameters_are_422(
        self, inventory_client: AsyncClient, query: str
    ) -> None:
        assert (await inventory_client.get(f"/_logs?{query}")).status_code == 422


class TestLogNoiseControl:
    """The ring buffer is small and the agent reads it; duplicates cost capacity."""

    def test_the_noisy_loggers_are_pinned_to_warning(self) -> None:
        import logging as stdlib_logging

        from services.common.logging_setup import NOISY_LIBRARIES

        configure_logging(service_name="svc", level="INFO")
        for name in NOISY_LIBRARIES:
            assert stdlib_logging.getLogger(name).level == stdlib_logging.WARNING, name

    def test_uvicorn_access_is_suppressed_in_favour_of_request_completed(self) -> None:
        """uvicorn.access duplicates our own access log, without a request id."""
        import logging as stdlib_logging

        from services.common.logging_setup import NOISY_LIBRARIES

        assert "uvicorn.access" in NOISY_LIBRARIES

        buffer = configure_logging(service_name="svc", level="INFO")
        stdlib_logging.getLogger("uvicorn.access").info(
            '172.18.0.1:42484 - "GET /orders/summary HTTP/1.1" 200'
        )
        assert buffer.tail(limit=10) == []

    def test_a_real_library_error_still_gets_through(self) -> None:
        """WARNING, not silence: a genuine connection failure must be visible."""
        import logging as stdlib_logging

        buffer = configure_logging(service_name="svc", level="INFO")
        stdlib_logging.getLogger("httpx").warning("connection pool is full")

        records = buffer.tail(limit=10)
        assert len(records) == 1
        assert records[0]["event"] == "connection pool is full"


class TestHealthAggregation:
    """The overall `status` field is what the monitor reads, so anything a
    dependency reports has to be reachable from it."""

    async def test_a_degraded_dependency_degrades_the_service(
        self, payments_app: tuple[FastAPI, ServiceContext], payments_client: AsyncClient
    ) -> None:
        _, context = payments_app

        async def reduced() -> dict[str, Any]:
            return {"name": "llm", "status": "degraded", "error": "running on a stub"}

        context.extra_health_checks.append(reduced)

        response = await payments_client.get("/health")
        body = response.json()

        assert response.status_code == 200  # still serving
        assert body["status"] == "degraded"
        assert "reduced capability: llm" in body["detail"]

    async def test_an_unhealthy_dependency_outranks_a_degraded_one(
        self, payments_app: tuple[FastAPI, ServiceContext], payments_client: AsyncClient
    ) -> None:
        _, context = payments_app

        async def reduced() -> dict[str, Any]:
            return {"name": "llm", "status": "degraded", "error": None}

        async def broken() -> dict[str, Any]:
            return {"name": "inventory", "status": "unhealthy", "error": "HTTP 503"}

        context.extra_health_checks.extend([reduced, broken])

        body = (await payments_client.get("/health")).json()
        assert body["status"] == "degraded"
        assert body["detail"] == "a dependency is unavailable"

    async def test_all_healthy_dependencies_leave_the_service_healthy(
        self, payments_app: tuple[FastAPI, ServiceContext], payments_client: AsyncClient
    ) -> None:
        _, context = payments_app

        async def fine() -> dict[str, Any]:
            return {"name": "llm", "status": "healthy", "error": None}

        context.extra_health_checks.append(fine)
        assert (await payments_client.get("/health")).json()["status"] == "healthy"
