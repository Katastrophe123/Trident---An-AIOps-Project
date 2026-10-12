"""Service-to-service HTTP client.

``orders`` is the only caller today, but the behaviour it needs is what makes
the chaos scenarios diagnosable, so it lives here rather than inline:

* **Failures are classified**, not just raised. "payments is down" (connect
  error) looks nothing like "payments declined this" (4xx) in the logs, and the
  agent has to tell them apart.
* **Every outcome is logged with the downstream service name and latency**, so
  the "slow dependency" scenario shows up as a latency attributed to
  ``inventory`` rather than a vague orders-is-slow.
* **No retries.** Retrying would mask the fault and multiply load during an
  incident; this demo wants the failure visible.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from services.common.logging_setup import get_logger
from services.common.metrics import MetricsRegistry

logger = get_logger(__name__)

FailureKind = Literal["unavailable", "timeout", "rejected", "server_error", "bad_response"]


class DownstreamError(Exception):
    """A call to another service did not succeed.

    ``kind`` is the classification the diagnosis depends on.
    """

    def __init__(
        self,
        service: str,
        kind: FailureKind,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"{service} {kind}: {message}")
        self.service = service
        self.kind = kind
        self.message = message
        self.status_code = status_code

    @property
    def is_dependency_down(self) -> bool:
        """True when the downstream could not be reached or errored, as opposed
        to having deliberately refused the request."""
        return self.kind in {"unavailable", "timeout", "server_error", "bad_response"}


@dataclass(frozen=True, slots=True)
class DownstreamResponse:
    payload: dict[str, Any]
    latency_ms: float


class ServiceClient:
    """Thin async client for one downstream service."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        timeout: float,
        metrics: MetricsRegistry | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._metrics = metrics
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def post(self, path: str, payload: dict[str, Any]) -> DownstreamResponse:
        """POST ``payload``, classifying every failure mode."""
        started = time.perf_counter()
        try:
            response = await self._client.post(path, json=payload)
        except httpx.TimeoutException as exc:
            self._record_failure("timeout", path, started)
            raise DownstreamError(self.name, "timeout", str(exc) or "timed out") from exc
        except httpx.TransportError as exc:
            # Connection refused / DNS failure: this is what a stopped
            # container looks like from the caller's side.
            self._record_failure("unavailable", path, started)
            raise DownstreamError(
                self.name, "unavailable", str(exc) or "could not connect"
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000

        if response.status_code >= 500:
            self._record_failure("server_error", path, started)
            raise DownstreamError(
                self.name,
                "server_error",
                response.text[:300],
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            # A deliberate refusal: not an outage, and must not be reported as one.
            logger.info(
                "downstream_rejected",
                extra={
                    "downstream": self.name,
                    "path": path,
                    "status_code": response.status_code,
                    "latency_ms": round(latency_ms, 2),
                },
            )
            raise DownstreamError(
                self.name, "rejected", response.text[:300], status_code=response.status_code
            )

        try:
            body = response.json()
        except ValueError as exc:
            self._record_failure("bad_response", path, started)
            raise DownstreamError(self.name, "bad_response", "response was not JSON") from exc

        logger.info(
            "downstream_ok",
            extra={
                "downstream": self.name,
                "path": path,
                "status_code": response.status_code,
                "latency_ms": round(latency_ms, 2),
            },
        )
        return DownstreamResponse(payload=body, latency_ms=latency_ms)

    async def health(self) -> tuple[bool, float | None, str | None]:
        """Return ``(ok, latency_ms, error)`` — used by the caller's own /health."""
        started = time.perf_counter()
        try:
            response = await self._client.get("/health")
        except httpx.HTTPError as exc:
            return False, None, f"{type(exc).__name__}: {exc}"[:200]
        latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code >= 400:
            return False, latency_ms, f"HTTP {response.status_code}"
        return True, latency_ms, None

    def _record_failure(self, kind: FailureKind, path: str, started: float) -> None:
        latency_ms = (time.perf_counter() - started) * 1000
        if self._metrics is not None:
            self._metrics.record_downstream_error(self.name)
        logger.error(
            "downstream_failed",
            extra={
                "downstream": self.name,
                "path": path,
                "failure_kind": kind,
                "latency_ms": round(latency_ms, 2),
            },
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
