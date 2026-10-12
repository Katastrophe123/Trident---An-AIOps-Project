#!/usr/bin/env python
"""Traffic generator: a steady stream of fake orders.

The plan asks for constant activity "so there is always something to observe".
That matters more than it sounds: without traffic, a broken service produces no
errors and no latency, so there is nothing for metrics to show or for the agent
to diagnose.

    python -m tools.traffic                      # ~2 orders/second, forever
    python -m tools.traffic --rate 5             # faster
    python -m tools.traffic --count 50           # then stop
    python -m tools.traffic --oneshot            # a single order, for tests

Also runs as a compose service, so ``docker compose up`` produces traffic with
no extra step.

Design notes:

* **Failures are expected, not fatal.** During a chaos scenario most requests
  fail; the generator logs and keeps going, because stopping would remove the
  very signal the demo needs.
* **A small share of orders are deliberately unservable** (a huge quantity, an
  unknown SKU) so there is a baseline of legitimate 4xx traffic. An agent that
  flags *those* as an incident is wrong, and this is what proves it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import signal
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

#: Matches the SKUs inventory seeds on startup.
SKUS: tuple[str, ...] = (
    "SKU-LAPTOP-13",
    "SKU-LAPTOP-15",
    "SKU-DOCK-USBC",
    "SKU-MOUSE-WL",
    "SKU-KEYB-MECH",
)

#: Share of orders that should legitimately be refused, so "some 4xx" is the
#: normal baseline rather than a signal.
UNSERVABLE_SHARE = 0.04

DEFAULT_ORDERS_URL = "http://localhost:8001"


@dataclass
class Stats:
    """Rolling counts, printed periodically and at exit."""

    started_at: float = field(default_factory=time.time)
    outcomes: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.outcomes.values())

    @property
    def elapsed(self) -> float:
        return max(time.time() - self.started_at, 1e-6)

    def record(self, outcome: str) -> None:
        self.outcomes[outcome] += 1

    def line(self) -> str:
        rate = self.total / self.elapsed
        parts = " ".join(f"{name}={count}" for name, count in sorted(self.outcomes.items()))
        return f"{self.total} sent ({rate:.1f}/s)  {parts}"


def build_order(rng: random.Random) -> dict[str, object]:
    """A normal order, or occasionally one that should be refused."""
    roll = rng.random()
    if roll < UNSERVABLE_SHARE / 2:
        # Unknown SKU -> inventory 404 -> a *rejected* order, not an incident.
        return {
            "customer_id": f"cust-{rng.randrange(10_000):04d}",
            "sku": "SKU-DOES-NOT-EXIST",
            "quantity": 1,
        }
    if roll < UNSERVABLE_SHARE:
        # Large order -> payment above the decline threshold -> rejected.
        return {
            "customer_id": f"cust-{rng.randrange(10_000):04d}",
            "sku": "SKU-LAPTOP-15",
            "quantity": 20,
        }
    return {
        "customer_id": f"cust-{rng.randrange(10_000):04d}",
        "sku": rng.choice(SKUS),
        "quantity": rng.randint(1, 3),
    }


def classify(response: httpx.Response) -> str:
    """Bucket a response for the stats line."""
    if response.status_code < 300:
        try:
            return str(response.json().get("status", "created"))
        except ValueError:
            return "created"
    if response.status_code == 502:
        return "http_502_dependency_down"
    if 400 <= response.status_code < 500:
        return f"http_{response.status_code}"
    return f"http_{response.status_code}"


async def send_one(client: httpx.AsyncClient, payload: dict[str, object], stats: Stats) -> None:
    """Send one order. Never raises - a failure is a data point."""
    try:
        response = await client.post("/orders", json=payload)
    except httpx.TimeoutException:
        stats.record("timeout")
        return
    except httpx.HTTPError:
        # orders itself is unreachable: the "crashed service" case, if the
        # crashed service happens to be orders.
        stats.record("unreachable")
        return
    stats.record(classify(response))


async def run(
    *,
    url: str,
    rate: float,
    count: int | None,
    report_every: float,
    seed: int | None,
) -> int:
    """Send orders at roughly ``rate`` per second until stopped."""
    rng = random.Random(seed)
    stats = Stats()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not available on Windows, and not needed there.
        with contextlib.suppress(NotImplementedError):  # pragma: no cover
            loop.add_signal_handler(sig, stop.set)

    interval = 1.0 / rate if rate > 0 else 0.0
    last_report = time.time()
    print(f"[traffic] sending to {url} at ~{rate}/s (Ctrl-C to stop)", flush=True)

    # A generous timeout on purpose: the slow-dependency scenario adds seconds
    # of latency, and timing out would hide that as a timeout rather than
    # showing it as the latency it is.
    async with httpx.AsyncClient(base_url=url, timeout=30.0) as client:
        in_flight: set[asyncio.Task[None]] = set()
        while not stop.is_set():
            if count is not None and stats.total + len(in_flight) >= count:
                break

            task = asyncio.create_task(send_one(client, build_order(rng), stats))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)

            if time.time() - last_report >= report_every:
                print(f"[traffic] {stats.line()}", flush=True)
                last_report = time.time()

            # The timeout *is* the pacing; a set event just ends the run early.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

    print(f"[traffic] stopped. {stats.line()}", flush=True)
    return 0


async def oneshot(url: str) -> int:
    """Send one known-good order and print the result.

    Deliberately not randomised: this is the smoke test, so it has to be the
    order that should always succeed. ``--seed`` only affects ``run()``.
    """
    payload = {"customer_id": "cust-0001", "sku": SKUS[0], "quantity": 1}
    async with httpx.AsyncClient(base_url=url, timeout=30.0) as client:
        try:
            response = await client.post("/orders", json=payload)
        except httpx.HTTPError as exc:
            print(f"[traffic] failed: {exc}", file=sys.stderr)
            return 2
    print(f"[traffic] HTTP {response.status_code} {json.dumps(response.json())[:300]}")
    return 0 if response.status_code < 400 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.traffic",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", default=DEFAULT_ORDERS_URL, help="orders-service base URL")
    parser.add_argument("--rate", type=float, default=2.0, help="Orders per second")
    parser.add_argument("--count", type=int, default=None, help="Stop after N orders")
    parser.add_argument("--report-every", type=float, default=10.0, help="Stats interval")
    parser.add_argument("--seed", type=int, default=None, help="Seed the order generator")
    parser.add_argument("--oneshot", action="store_true", help="Send one order and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.oneshot:
        return asyncio.run(oneshot(args.url))
    return asyncio.run(
        run(
            url=args.url,
            rate=args.rate,
            count=args.count,
            report_every=args.report_every,
            seed=args.seed,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
