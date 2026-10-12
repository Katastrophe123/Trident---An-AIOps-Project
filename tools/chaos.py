#!/usr/bin/env python
"""chaos.py — inject one failure at a time, and put it back.

One subcommand per scenario from the project plan, plus ``reset``. Each has a
single known root cause, so the agent can be scored against it in Phase 3.

    python -m tools.chaos status
    python -m tools.chaos crash            --service payments
    python -m tools.chaos bad-config       --service payments
    python -m tools.chaos slow-dependency  --service inventory --seconds 3
    python -m tools.chaos memory-leak      --service orders
    python -m tools.chaos db-exhaustion    --service payments --connections 20
    python -m tools.chaos reset

Two mechanisms, chosen per scenario:

* **Container-level** (crash, bad config) go through ``docker compose``, because
  stopping a container and recreating it with different configuration *is* a
  compose operation.
* **In-process** (slow dependency, memory leak, DB exhaustion) go through each
  service's own ``/_chaos`` endpoints, because they need to change behaviour
  inside a live process without a restart.

``reset`` undoes both kinds and restocks inventory, so a demo can be run
repeatedly from a known-good state.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

from tools import compose

#: Host ports published by docker-compose.yml.
SERVICE_PORTS: dict[str, int] = {
    "orders": 8001,
    "payments": 8002,
    "inventory": 8003,
}
CONSOLE_PORT = 8080

#: The wrong password the "bad config" scenario injects.
WRONG_PASSWORD = "definitely-not-the-password"

HTTP_TIMEOUT = 10.0


def service_url(service: str) -> str:
    try:
        return f"http://localhost:{SERVICE_PORTS[service]}"
    except KeyError:
        raise SystemExit(
            f"Unknown service {service!r}. Choose from: {', '.join(SERVICE_PORTS)}"
        ) from None


@dataclass
class Reporter:
    """Prints progress. Quiet mode is for the tests, which assert on results."""

    quiet: bool = False

    def say(self, message: str) -> None:
        if not self.quiet:
            print(message)

    def step(self, message: str) -> None:
        self.say(f"  → {message}")

    def ok(self, message: str) -> None:
        self.say(f"  ✓ {message}")

    def warn(self, message: str) -> None:
        self.say(f"  ! {message}")

    def heading(self, message: str) -> None:
        self.say(f"\n{message}")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def post_chaos(service: str, path: str, *, timeout: float = HTTP_TIMEOUT) -> dict[str, Any]:
    """POST to a service's own chaos endpoint."""
    url = f"{service_url(service)}/_chaos{path}"
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url)
        response.raise_for_status()
        return dict(response.json())


def get_json(service: str, path: str, *, timeout: float = 5.0) -> dict[str, Any] | None:
    """GET from a service, returning None when it cannot be reached."""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{service_url(service)}{path}")
            if response.status_code >= 400 and response.status_code != 503:
                return None
            return dict(response.json())
    except (httpx.HTTPError, ValueError):
        return None


def wait_for_health(
    service: str, *, expect_reachable: bool, attempts: int = 30, delay: float = 1.0
) -> bool:
    """Poll /health until the service is (or is not) answering."""
    for _ in range(attempts):
        reachable = get_json(service, "/health") is not None
        if reachable == expect_reachable:
            return True
        time.sleep(delay)
    return False


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def scenario_crash(service: str, reporter: Reporter) -> int:
    """Crashed service: stop the container.

    Root cause: the container is not running. Symptom: callers get connection
    errors, so orders reports ``failed`` orders and 502s.
    """
    reporter.heading(f"Injecting: crashed service ({service})")
    compose.require_compose()
    reporter.step(f"docker compose stop {service}")
    compose.run("stop", service)
    if wait_for_health(service, expect_reachable=False, attempts=15):
        reporter.ok(f"{service} is no longer answering")
    else:
        reporter.warn(f"{service} still answers - it may not have stopped")
    reporter.say(
        f"\n  Expect: orders /health degraded, 502s on POST /orders, "
        f"'{service} unavailable' in orders' logs."
    )
    reporter.say(f"  Fix by hand: docker compose start {service}")
    return 0


def scenario_bad_config(service: str, reporter: Reporter) -> int:
    """Bad config: recreate the container with a wrong database password.

    Root cause: ``DATABASE_URL`` has the wrong credentials. Symptom:
    authentication errors in the logs and an unhealthy /health, while the
    container itself is up - which is what distinguishes it from a crash.
    """
    reporter.heading(f"Injecting: bad config ({service})")
    compose.require_compose()

    # Per-service override, so only the targeted service gets the bad password.
    key = f"{service.upper()}_DATABASE_URL"
    bad_url = f"postgresql+asyncpg://aiops_app:{WRONG_PASSWORD}@db:5432/aiops"
    reporter.step(f"writing {key} with a wrong password to {compose.OVERRIDE_FILE.name}")
    compose.write_overrides({key: bad_url})

    reporter.step(f"docker compose up -d --force-recreate --no-deps {service}")
    compose.run("up", "-d", "--force-recreate", "--no-deps", service)

    if wait_for_health(service, expect_reachable=True, attempts=30):
        health = get_json(service, "/health") or {}
        status = health.get("status")
        reporter.ok(f"{service} is up and reporting status={status!r}")
        if status == "healthy":
            reporter.warn(
                "Expected unhealthy. The override may not have been picked up - "
                "check that docker-compose.yml lists .chaos/overrides.env in env_file."
            )
    else:
        reporter.warn(f"{service} did not come back up")

    reporter.say(
        "\n  Expect: /health unhealthy with a password authentication error, "
        "and 'database_healthcheck_failed' in the logs."
    )
    reporter.say("  Fix by hand: python -m tools.chaos reset")
    return 0


def scenario_slow_dependency(service: str, seconds: float, reporter: Reporter) -> int:
    """Slow dependency: add a delay to every request the service handles.

    Root cause: the service is slow. Symptom: orders' latency climbs and its
    ``downstream_errors``/dependency latency points at this service - not at
    orders itself.
    """
    milliseconds = int(seconds * 1000)
    reporter.heading(f"Injecting: slow dependency ({service}, {seconds}s)")
    state = post_chaos(
        service,
        f"/latency?milliseconds={milliseconds}",
        timeout=HTTP_TIMEOUT + seconds,
    )
    reporter.ok(f"{service} now delays every request by {state['latency_ms']} ms")
    reporter.say(
        "\n  Expect: orders' recent_average_latency_ms climbs above "
        f"{milliseconds} ms, and {service} reports status=degraded."
    )
    reporter.say(
        f"  Fix by hand: python -m tools.chaos slow-dependency --service {service} --seconds 0"
    )
    return 0


def scenario_memory_leak(service: str, reporter: Reporter) -> int:
    """Memory leak: a background task that keeps appending to a list.

    Root cause: unbounded growth in the process. Symptom: ``memory_mb`` climbs
    monotonically while request metrics stay normal - the hardest scenario to
    diagnose, because nothing is failing yet.
    """
    reporter.heading(f"Injecting: memory leak ({service})")
    state = post_chaos(service, "/memory-leak?enabled=true")
    reporter.ok(f"{service} is leaking (4 MiB every 0.5s, {state['leaked_mb']} MiB so far)")
    reporter.say(
        "\n  Expect: memory_mb on /metrics climbing steadily; "
        "'chaos_memory_leak_growing' warnings in the logs."
    )
    reporter.say(
        f"  Fix by hand: docker compose restart {service}  "
        f"(a real leak is not reclaimed by stopping the leaking code)"
    )
    return 0


def scenario_db_exhaustion(service: str, connections: int, reporter: Reporter) -> int:
    """Database exhaustion: hold many raw connections open.

    Root cause: connections are held and never returned. Symptom: *other*
    services start failing to reach the database, so the service at fault is
    not the service reporting the error.
    """
    reporter.heading(f"Injecting: database exhaustion ({service}, {connections} connections)")
    state = post_chaos(service, f"/db-exhaust?connections={connections}", timeout=HTTP_TIMEOUT + 20)
    held = state["held_db_connections"]
    reporter.ok(f"{service} is holding {held} connections")
    if held < connections:
        reporter.step(f"Postgres refused after {held} - that refusal is itself the symptom")
    reporter.say(
        "\n  Expect: 'too many connections' or pool-timeout errors on the OTHER "
        "services, and their /health going unhealthy."
    )
    reporter.say(
        f"  Fix by hand: python -m tools.chaos db-exhaustion --service {service} --connections 0"
    )
    return 0


def scenario_reset(reporter: Reporter) -> int:
    """Restore a healthy system: clear overrides, recreate, clear in-process faults."""
    reporter.heading("Resetting to a healthy system")
    compose.require_compose()

    # 1. container-level: drop any bad config and bring everything back up.
    if compose.clear_overrides():
        reporter.step("cleared .chaos/overrides.env")
    else:
        reporter.step("no config overrides were set")

    reporter.step(
        "docker compose up -d --force-recreate --no-deps orders payments inventory console"
    )
    compose.run(
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "orders",
        "payments",
        "inventory",
        "console",
    )

    # 2. wait for each service, then clear in-process faults.
    for service in SERVICE_PORTS:
        if not wait_for_health(service, expect_reachable=True, attempts=40):
            reporter.warn(f"{service} did not come back - check `docker compose logs {service}`")
            continue
        try:
            state = post_chaos(service, "/reset?reset_metrics=true")
            flags = "clean" if state["healthy"] else json.dumps(state)
            reporter.ok(f"{service} reset ({flags})")
        except httpx.HTTPError as exc:
            reporter.warn(f"{service} chaos reset failed: {exc}")

    # 3. put the stock back, so long runs cannot drain it.
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            client.post(f"{service_url('inventory')}/stock/restock").raise_for_status()
        reporter.ok("inventory restocked")
    except httpx.HTTPError as exc:
        reporter.warn(f"restock failed: {exc}")

    healthy = print_status(Reporter(quiet=True))
    reporter.say("")
    if healthy:
        reporter.ok("every service reports healthy")
        return 0
    reporter.warn("some services are not healthy yet - re-run `python -m tools.chaos status`")
    return 1


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def print_status(reporter: Reporter) -> bool:
    """Print one line per service. Returns True when all are healthy."""
    reporter.heading("Demo system status")
    try:
        compose.require_compose()
        running = compose.running_services()
    except compose.ComposeError:
        running = set()

    all_healthy = True
    for service in SERVICE_PORTS:
        health = get_json(service, "/health")
        metrics = get_json(service, "/metrics") or {}
        chaos = get_json(service, "/_chaos") or {}

        if health is None:
            all_healthy = False
            container = "running" if service in running else "stopped"
            reporter.say(f"  {service:<10} UNREACHABLE   (container {container})")
            continue

        status = str(health.get("status"))
        if status != "healthy":
            all_healthy = False

        faults = []
        if chaos.get("latency_ms"):
            faults.append(f"delay {chaos['latency_ms']}ms")
        if chaos.get("memory_leak_active"):
            faults.append(f"leaking {chaos.get('leaked_mb', 0)}MiB")
        if chaos.get("held_db_connections"):
            faults.append(f"{chaos['held_db_connections']} held conns")

        reporter.say(
            f"  {service:<10} {status.upper():<13} "
            f"req={metrics.get('requests', 0):<6} "
            f"err={metrics.get('errors', 0):<5} "
            f"lat={metrics.get('recent_average_latency_ms', 0):>7.1f}ms "
            f"mem={metrics.get('memory_mb', 0):>6.1f}MB"
            + (f"  [{', '.join(faults)}]" if faults else "")
        )
        if health.get("detail"):
            reporter.say(f"             detail: {health['detail']}")

    console = get_json("orders", "/orders/summary")
    if console:
        reporter.say(f"\n  orders by status: {console}")
    reporter.say(f"  console: http://localhost:{CONSOLE_PORT}")
    return all_healthy


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.chaos",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show health, metrics and injected faults")

    crash = sub.add_parser("crash", help="Scenario 1: stop a service's container")
    crash.add_argument("--service", default="payments", choices=sorted(SERVICE_PORTS))

    bad = sub.add_parser("bad-config", help="Scenario 2: wrong database password")
    bad.add_argument("--service", default="payments", choices=sorted(SERVICE_PORTS))

    slow = sub.add_parser("slow-dependency", help="Scenario 3: add request latency")
    slow.add_argument("--service", default="inventory", choices=sorted(SERVICE_PORTS))
    slow.add_argument("--seconds", type=float, default=3.0, help="0 clears the delay")

    leak = sub.add_parser("memory-leak", help="Scenario 4: unbounded memory growth")
    leak.add_argument("--service", default="orders", choices=sorted(SERVICE_PORTS))

    exhaust = sub.add_parser("db-exhaustion", help="Scenario 5: hold DB connections")
    exhaust.add_argument("--service", default="payments", choices=sorted(SERVICE_PORTS))
    exhaust.add_argument("--connections", type=int, default=20, help="0 releases them")

    sub.add_parser("reset", help="Restore a healthy system")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    reporter = Reporter(quiet=args.quiet)

    try:
        if args.command == "status":
            return 0 if print_status(reporter) else 1
        if args.command == "crash":
            return scenario_crash(args.service, reporter)
        if args.command == "bad-config":
            return scenario_bad_config(args.service, reporter)
        if args.command == "slow-dependency":
            return scenario_slow_dependency(args.service, args.seconds, reporter)
        if args.command == "memory-leak":
            return scenario_memory_leak(args.service, reporter)
        if args.command == "db-exhaustion":
            return scenario_db_exhaustion(args.service, args.connections, reporter)
        if args.command == "reset":
            return scenario_reset(reporter)
    except compose.ComposeError as exc:
        print(f"\nDocker error: {exc}", file=sys.stderr)
        return 2
    except httpx.HTTPError as exc:
        print(
            f"\nCould not reach a service: {exc}\nIs the stack up? Try `docker compose up -d`.",
            file=sys.stderr,
        )
        return 2

    return 0  # pragma: no cover - argparse requires a subcommand


if __name__ == "__main__":
    raise SystemExit(main())
