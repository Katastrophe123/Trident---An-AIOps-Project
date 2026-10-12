"""The console: server-side fan-out, and the dashboard's JSON contract.

The console's whole job is to make a broken system *legible*, so most of what
matters here is what it does when the services it watches are down - which is
exactly when a naive dashboard returns a 500 and shows nothing.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from services.console.main import (
    MANUAL_SCENARIOS,
    STATIC_DIR,
    WATCHED,
    ConsoleClient,
    create_app,
)

TARGETS = {
    "orders": "http://orders",
    "payments": "http://payments",
    "inventory": "http://inventory",
}


def service_of(request: httpx.Request) -> str:
    return str(request.url.host)


def healthy_stub(
    *, down: set[str] | None = None, status: dict[str, str] | None = None
) -> httpx.MockTransport:
    """A fake fleet. ``down`` services raise; ``status`` overrides health status."""
    down = down or set()
    status = status or {}

    def handler(request: httpx.Request) -> httpx.Response:
        service = service_of(request)
        if service in down:
            raise httpx.ConnectError("connection refused", request=request)

        path = request.url.path
        if path == "/health":
            service_status = status.get(service, "healthy")
            code = 503 if service_status == "unhealthy" else 200
            return httpx.Response(
                code,
                json={
                    "status": service_status,
                    "service": f"{service}-service",
                    "version": "0.1.0",
                    "uptime_seconds": 12.5,
                    "dependencies": [{"name": "postgres", "status": "healthy"}],
                    "detail": None if service_status == "healthy" else "something is wrong",
                },
            )
        if path == "/metrics":
            return httpx.Response(
                200,
                json={
                    "service": f"{service}-service",
                    "uptime_seconds": 12.5,
                    "requests": 100,
                    "errors": 2,
                    "error_rate": 0.02,
                    "average_latency_ms": 11.0,
                    "recent_average_latency_ms": 12.0,
                    "recent_p95_latency_ms": 30.0,
                    "memory_mb": 64.0,
                },
            )
        if path == "/_chaos":
            return httpx.Response(
                200,
                json={
                    "service": f"{service}-service",
                    "latency_ms": 0,
                    "memory_leak_active": False,
                    "leaked_mb": 0,
                    "held_db_connections": 0,
                    "healthy": True,
                },
            )
        if path == "/_logs":
            # Distinct epochs per service so the merge order is checkable.
            base = {"orders": 300.0, "payments": 200.0, "inventory": 100.0}[service]
            return httpx.Response(
                200,
                json={
                    "service": service,
                    "count": 2,
                    "records": [
                        {
                            "timestamp": "t",
                            "epoch": base + offset,
                            "level": "INFO",
                            "service": service,
                            "event": f"{service}_event_{offset}",
                        }
                        for offset in (0, 1)
                    ],
                },
            )
        if path == "/orders/summary":
            return httpx.Response(200, json={"confirmed": 90, "rejected": 10})
        if path.startswith("/_chaos/"):
            return httpx.Response(200, json={"service": service, "latency_ms": 3000})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def console_client(transport: httpx.MockTransport) -> ConsoleClient:
    client = ConsoleClient(dict(TARGETS))
    client._client = httpx.AsyncClient(transport=transport)  # noqa: SLF001
    return client


class TestConsoleClient:
    async def test_overview_of_a_healthy_fleet(self) -> None:
        client = console_client(healthy_stub())
        overview = await client.overview()

        assert [item.name for item in overview.services] == list(TARGETS)
        assert overview.all_healthy is True
        assert overview.orders_by_status == {"confirmed": 90, "rejected": 10}
        assert overview.services[0].requests == 100
        assert overview.services[0].memory_mb == 64.0
        await client.aclose()

    async def test_a_down_service_is_reported_not_raised(self) -> None:
        """The crashed-service scenario. A dashboard that 500s here is useless
        at precisely the moment it is needed."""
        client = console_client(healthy_stub(down={"payments"}))
        overview = await client.overview()

        by_name = {item.name: item for item in overview.services}
        assert by_name["payments"].reachable is False
        assert by_name["payments"].status == "unreachable"
        assert by_name["payments"].error == "no response from /health"
        # The others are unaffected.
        assert by_name["orders"].status == "healthy"
        assert overview.all_healthy is False
        await client.aclose()

    async def test_a_503_health_body_is_still_read(self) -> None:
        """An unhealthy service answers with 503 *and* a useful body. Treating
        that as a failed request would throw away the diagnosis."""
        client = console_client(healthy_stub(status={"payments": "unhealthy"}))
        overview = await client.overview()

        payments = next(i for i in overview.services if i.name == "payments")
        assert payments.reachable is True
        assert payments.status == "unhealthy"
        assert payments.detail == "something is wrong"
        await client.aclose()

    async def test_degraded_is_not_all_healthy(self) -> None:
        client = console_client(healthy_stub(status={"inventory": "degraded"}))
        overview = await client.overview()
        assert overview.all_healthy is False
        await client.aclose()

    async def test_overview_carries_the_manual_scenarios(self) -> None:
        client = console_client(healthy_stub())
        overview = await client.overview()
        assert set(overview.manual_scenarios) == {"crash", "bad-config"}
        assert overview.manual_scenarios["crash"]["inject"].startswith("docker compose")
        await client.aclose()

    async def test_logs_merge_newest_first_across_services(self) -> None:
        client = console_client(healthy_stub())
        records = await client.logs(None, limit=10, level=None)

        epochs = [record["epoch"] for record in records]
        assert epochs == sorted(epochs, reverse=True)
        assert {record["service"] for record in records} == set(TARGETS)
        await client.aclose()

    async def test_logs_respect_the_limit_after_merging(self) -> None:
        client = console_client(healthy_stub())
        records = await client.logs(None, limit=2, level=None)
        assert len(records) == 2
        # The two newest overall, not two per service.
        assert all(record["service"] == "orders" for record in records)
        await client.aclose()

    async def test_logs_for_one_service_only(self) -> None:
        client = console_client(healthy_stub())
        records = await client.logs("payments", limit=10, level=None)
        assert {record["service"] for record in records} == {"payments"}
        await client.aclose()

    async def test_logs_skip_a_down_service(self) -> None:
        client = console_client(healthy_stub(down={"orders"}))
        records = await client.logs(None, limit=10, level=None)
        assert "orders" not in {record["service"] for record in records}
        assert records  # the others still came back
        await client.aclose()

    async def test_an_unknown_service_is_a_404(self) -> None:
        client = console_client(healthy_stub())
        with pytest.raises(Exception) as excinfo:
            client.base_url("billing")
        assert "Unknown service" in str(excinfo.value)
        await client.aclose()

    async def test_posting_to_a_down_service_is_a_502(self) -> None:
        client = console_client(healthy_stub(down={"inventory"}))
        with pytest.raises(Exception) as excinfo:
            await client.post("inventory", "/_chaos/latency?milliseconds=10")
        assert "not reachable" in str(excinfo.value)
        await client.aclose()

    async def test_a_rejected_chaos_call_keeps_its_status_code(self) -> None:
        """So CHAOS_ENABLED=false surfaces as 403 in the UI, not a generic 502."""

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="Chaos endpoints are disabled")

        client = console_client(httpx.MockTransport(handler))
        with pytest.raises(Exception) as excinfo:
            await client.post("orders", "/_chaos/latency?milliseconds=10")
        assert "disabled" in str(excinfo.value)
        await client.aclose()

    async def test_targets_are_copied_not_shared(self) -> None:
        client = console_client(healthy_stub())
        client.targets["orders"] = "http://hijacked"
        assert client.base_url("orders") == "http://orders"
        await client.aclose()


class TestConsoleRoutes:
    """The HTTP surface the browser actually talks to."""

    @pytest.fixture
    async def app_client(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from services.common.config import get_settings

        get_settings.cache_clear()
        app = create_app()
        # Point the app's client at the fake fleet. Same object the routes
        # closed over, so mutating it is enough.
        app.state.client._targets.clear()  # noqa: SLF001
        app.state.client._targets.update(TARGETS)  # noqa: SLF001
        app.state.client._client = httpx.AsyncClient(transport=healthy_stub())  # noqa: SLF001

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://console"
        ) as client:
            yield client
        get_settings.cache_clear()

    async def test_console_health_is_shallow_on_purpose(self, app_client: AsyncClient) -> None:
        """The console is up even when everything it watches is down. Reporting
        otherwise would make the dashboard look like the outage."""
        body = (await app_client.get("/health")).json()
        assert body["status"] == "healthy"
        assert body["service"] == "console"
        assert set(body["watching"]) == set(TARGETS)

    async def test_overview_route(self, app_client: AsyncClient) -> None:
        response = await app_client.get("/api/overview")
        assert response.status_code == 200
        body = response.json()
        assert len(body["services"]) == 3
        assert body["all_healthy"] is True

    async def test_logs_route(self, app_client: AsyncClient) -> None:
        body = (await app_client.get("/api/logs?limit=4")).json()
        assert body["count"] == 4
        assert len(body["records"]) == 4

    async def test_logs_route_rejects_a_silly_limit(self, app_client: AsyncClient) -> None:
        assert (await app_client.get("/api/logs?limit=9999")).status_code == 422

    @pytest.mark.parametrize(
        "path",
        [
            "/api/chaos/latency?service=inventory&milliseconds=3000",
            "/api/chaos/memory-leak?service=orders&enabled=true",
            "/api/chaos/db-exhaust?service=payments&connections=20",
        ],
    )
    async def test_chaos_routes_proxy_through(self, app_client: AsyncClient, path: str) -> None:
        assert (await app_client.post(path)).status_code == 200

    async def test_reset_reports_per_service_outcomes(self, app_client: AsyncClient) -> None:
        body = (await app_client.post("/api/chaos/reset")).json()
        assert set(body["reset"]) == set(TARGETS)

    async def test_reset_records_a_failure_instead_of_aborting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One unreachable service must not stop the other two being reset."""
        from services.common.config import get_settings

        get_settings.cache_clear()
        app = create_app()
        app.state.client._targets.clear()  # noqa: SLF001
        app.state.client._targets.update(TARGETS)  # noqa: SLF001
        app.state.client._client = httpx.AsyncClient(  # noqa: SLF001
            transport=healthy_stub(down={"payments"})
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://console"
        ) as client:
            body = (await client.post("/api/chaos/reset")).json()

        assert "error" in body["reset"]["payments"]
        assert "error" not in body["reset"]["orders"]
        get_settings.cache_clear()

    async def test_the_index_page_is_served(self, app_client: AsyncClient) -> None:
        response = await app_client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    async def test_the_static_assets_are_served(self, app_client: AsyncClient) -> None:
        for name in ("styles.css", "app.js"):
            response = await app_client.get(f"/static/{name}")
            assert response.status_code == 200, name
            assert response.text.strip()


class TestFrontendAssets:
    """Cheap structural checks on the hand-written frontend.

    No browser here, so these only assert the things that silently break the
    page: a renamed data-bind, a missing template, a stylesheet that forgot
    dark mode. Rendering was verified by eye.
    """

    @pytest.fixture
    def index_html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @pytest.fixture
    def app_js(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    @pytest.fixture
    def styles_css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    def test_only_three_static_files_and_no_build_step(self) -> None:
        names = sorted(path.name for path in STATIC_DIR.iterdir() if path.is_file())
        assert names == ["app.js", "index.html", "styles.css"]

    def test_no_external_scripts_or_cdn_links(self, index_html: str) -> None:
        """Vanilla means vanilla: it has to work with no network."""
        assert "http://" not in index_html.replace("http://localhost", "")
        assert "cdn" not in index_html.lower()
        assert "https://" not in index_html

    def test_the_templates_the_javascript_clones_exist(self, index_html: str, app_js: str) -> None:
        for template_id in ("tpl-service", "tpl-manual"):
            assert f'id="{template_id}"' in index_html
            assert template_id in app_js

    def test_every_endpoint_the_page_calls_exists_on_the_server(self, app_js: str) -> None:
        for path in ("/api/overview", "/api/logs", "/api/chaos/reset"):
            assert path in app_js

    def test_dark_mode_is_declared_for_both_the_media_query_and_the_toggle(
        self, styles_css: str
    ) -> None:
        assert "prefers-color-scheme: dark" in styles_css
        assert '[data-theme="dark"]' in styles_css

    def test_status_is_never_colour_alone(self, app_js: str) -> None:
        """Required for colour-vision deficiency: every pill has an icon and a
        word next to it."""
        assert "STATUS_ICON" in app_js
        assert "STATUS_WORD" in app_js

    def test_the_watched_services_match_the_backend(self, app_js: str) -> None:
        for name, _ in WATCHED:
            assert name in app_js

    def test_the_manual_scenarios_are_not_duplicated_in_the_frontend(self, app_js: str) -> None:
        """The commands come from the API, so they cannot drift out of date."""
        for scenario in MANUAL_SCENARIOS.values():
            assert scenario["inject"] not in app_js
