"""In-process metrics: request count, error count, average latency, memory.

Exactly the four readings the plan asks for, kept deliberately simple so the
agent's ``get_metrics`` tool (Phase 2) has an obvious contract to read.

Latency is tracked two ways:

* a running total, giving the average over the process lifetime; and
* a bounded window of recent samples, giving a *recent* average and p95.

The window matters for the "slow dependency" scenario: a lifetime average over
thousands of fast requests barely moves when latency spikes, so an agent reading
only that would miss the incident. The recent window moves immediately.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

import psutil

#: Recent-latency window. ~200 requests is a few seconds of demo traffic.
RECENT_WINDOW = 200


@dataclass
class EndpointStats:
    """Per-endpoint counters, so a spike can be attributed to a route."""

    requests: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0

    @property
    def average_latency_ms(self) -> float:
        return self.total_latency_ms / self.requests if self.requests else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "errors": self.errors,
            "average_latency_ms": round(self.average_latency_ms, 2),
        }


@dataclass
class MetricsRegistry:
    """Thread-safe counters for one service process."""

    service_name: str
    started_at: float = field(default_factory=time.time)
    requests: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0
    status_counts: dict[str, int] = field(default_factory=dict)
    endpoints: dict[str, EndpointStats] = field(default_factory=dict)
    #: Downstream call failures, so orders can report "payments is refusing".
    downstream_errors: dict[str, int] = field(default_factory=dict)
    _recent_latencies: deque[float] = field(default_factory=lambda: deque(maxlen=RECENT_WINDOW))
    _lock: Lock = field(default_factory=Lock)

    # --- recording --------------------------------------------------------
    def record_request(self, *, endpoint: str, status_code: int, latency_ms: float) -> None:
        is_error = status_code >= 500
        bucket = f"{status_code // 100}xx"
        with self._lock:
            self.requests += 1
            self.total_latency_ms += latency_ms
            self._recent_latencies.append(latency_ms)
            self.status_counts[bucket] = self.status_counts.get(bucket, 0) + 1
            if is_error:
                self.errors += 1

            stats = self.endpoints.setdefault(endpoint, EndpointStats())
            stats.requests += 1
            stats.total_latency_ms += latency_ms
            if is_error:
                stats.errors += 1

    def record_downstream_error(self, service: str) -> None:
        with self._lock:
            self.downstream_errors[service] = self.downstream_errors.get(service, 0) + 1

    def reset(self) -> None:
        """Zero every counter. Used by `chaos.py reset` and by tests."""
        with self._lock:
            self.requests = 0
            self.errors = 0
            self.total_latency_ms = 0.0
            self.status_counts.clear()
            self.endpoints.clear()
            self.downstream_errors.clear()
            self._recent_latencies.clear()
            self.started_at = time.time()

    # --- derived ----------------------------------------------------------
    @property
    def average_latency_ms(self) -> float:
        with self._lock:
            return self.total_latency_ms / self.requests if self.requests else 0.0

    @property
    def recent_average_latency_ms(self) -> float:
        with self._lock:
            samples = list(self._recent_latencies)
        return sum(samples) / len(samples) if samples else 0.0

    @property
    def recent_p95_latency_ms(self) -> float:
        with self._lock:
            samples = sorted(self._recent_latencies)
        if not samples:
            return 0.0
        index = min(len(samples) - 1, int(len(samples) * 0.95))
        return samples[index]

    @property
    def error_rate(self) -> float:
        with self._lock:
            return self.errors / self.requests if self.requests else 0.0

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at

    @staticmethod
    def memory_mb() -> float:
        """Resident set size of this process, in MiB.

        This is the reading that climbs during the memory-leak scenario.
        """
        return psutil.Process().memory_info().rss / (1024 * 1024)

    def snapshot(self) -> dict[str, Any]:
        """The payload returned by ``GET /metrics``."""
        with self._lock:
            endpoints = {name: stats.to_dict() for name, stats in self.endpoints.items()}
            status_counts = dict(self.status_counts)
            downstream_errors = dict(self.downstream_errors)

        return {
            "service": self.service_name,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "requests": self.requests,
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "average_latency_ms": round(self.average_latency_ms, 2),
            "recent_average_latency_ms": round(self.recent_average_latency_ms, 2),
            "recent_p95_latency_ms": round(self.recent_p95_latency_ms, 2),
            "memory_mb": round(self.memory_mb(), 2),
            "status_counts": status_counts,
            "downstream_errors": downstream_errors,
            "endpoints": endpoints,
        }
