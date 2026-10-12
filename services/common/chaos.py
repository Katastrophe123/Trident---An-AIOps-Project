"""In-process fault injection for three of the five chaos scenarios.

The other two (crashed service, bad config) are container-level and are driven
by ``tools/chaos.py`` through the Docker Compose CLI, because that is what a
human would type and what the RUNBOOK documents.

The three handled here need to happen *inside* a running process:

================  ==================================================
Scenario          Mechanism
================  ==================================================
Slow dependency   middleware sleeps ``latency_ms`` before handling
Memory leak       a background task appends 1 MiB chunks to a list
DB exhaustion     raw asyncpg connections opened and held open
================  ==================================================

Every endpoint that drives this is gated behind ``CHAOS_ENABLED``. A real
service would obviously never expose a fault-injection API; this one exists so
the demo can be broken reproducibly from a script or a button.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from services.common.logging_setup import get_logger

logger = get_logger(__name__)

#: Size of each chunk the memory leak appends.
LEAK_CHUNK_MB = 4
#: How often the leak task appends another chunk.
LEAK_INTERVAL_SECONDS = 0.5
#: Hard ceiling so a forgotten leak cannot take the whole machine down.
LEAK_CEILING_MB = 1024


@dataclass
class ChaosState:
    """Mutable fault-injection state for one service process."""

    service_name: str
    #: Artificial delay added to every request, in milliseconds.
    latency_ms: int = 0
    #: Whether the memory-leak task is running.
    memory_leak_active: bool = False

    _leaked: list[bytearray] = field(default_factory=list)
    _leak_task: asyncio.Task[None] | None = None
    _held_connections: list[Any] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # --- slow dependency --------------------------------------------------
    def set_latency(self, milliseconds: int) -> None:
        """Set (or clear, with 0) the artificial per-request delay."""
        self.latency_ms = max(0, milliseconds)
        logger.warning(
            "chaos_latency_set",
            extra={"chaos_scenario": "slow_dependency", "latency_ms": self.latency_ms},
        )

    async def apply_latency(self) -> None:
        """Awaited by the middleware on every request."""
        if self.latency_ms > 0:
            await asyncio.sleep(self.latency_ms / 1000)

    # --- memory leak ------------------------------------------------------
    @property
    def leaked_mb(self) -> int:
        return len(self._leaked) * LEAK_CHUNK_MB

    async def _leak_loop(self) -> None:
        """Append a chunk every interval until stopped or the ceiling is hit."""
        try:
            while self.leaked_mb < LEAK_CEILING_MB:
                # bytearray of non-zero bytes so the pages are really resident.
                self._leaked.append(bytearray(b"\xab" * LEAK_CHUNK_MB * 1024 * 1024))
                logger.warning(
                    "chaos_memory_leak_growing",
                    extra={"chaos_scenario": "memory_leak", "leaked_mb": self.leaked_mb},
                )
                await asyncio.sleep(LEAK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        finally:
            if self.leaked_mb >= LEAK_CEILING_MB:
                logger.error(
                    "chaos_memory_leak_ceiling_reached",
                    extra={"chaos_scenario": "memory_leak", "leaked_mb": self.leaked_mb},
                )

    async def start_memory_leak(self) -> None:
        async with self._lock:
            if self.memory_leak_active:
                return
            self.memory_leak_active = True
            self._leak_task = asyncio.create_task(self._leak_loop())
            logger.warning("chaos_memory_leak_started", extra={"chaos_scenario": "memory_leak"})

    async def stop_memory_leak(self, *, free: bool = True) -> None:
        """Stop the leak. ``free`` releases the already-leaked memory.

        A real leak is not reclaimed by stopping the leaking code, which is why
        the RUNBOOK's manual fix is "restart the container". ``free=True`` is
        here so `chaos.py reset` can return to a clean state without a restart.
        """
        async with self._lock:
            self.memory_leak_active = False
            if self._leak_task is not None:
                self._leak_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._leak_task
                self._leak_task = None
            if free:
                self._leaked.clear()
            logger.warning(
                "chaos_memory_leak_stopped",
                extra={"chaos_scenario": "memory_leak", "freed": free},
            )

    # --- database exhaustion ---------------------------------------------
    async def exhaust_connections(self, count: int, dsn: str) -> int:
        """Open ``count`` raw connections, hold them, and evict everyone else.

        Raw asyncpg rather than the SQLAlchemy pool on purpose: the pool would
        queue and reuse, which is the opposite of what this simulates. Returns
        how many were actually opened - fewer than asked for means the server
        refused, which is itself the symptom.

        The eviction step is what makes this scenario visible, and it took three
        failed attempts to work out why holding connections alone did nothing:

        1. The services connected as a **superuser**, which PostgreSQL never
           holds to ``max_connections``. Fixed by adding a NOSUPERUSER role
           (``docker/initdb``).
        2. Even then, every service's pool was already warm and simply reused
           the connections it held, so nobody ever asked for a new one.
        3. Adding ``pool_recycle`` did not help either: SQLAlchemy recycles by
           closing the old connection *before* opening its replacement, so the
           replacement always fits in the slot just freed.

        An exhausted database only hurts when something needs an *additional*
        connection. At a couple of orders a second, nothing ever does. So after
        taking the slots, this terminates the other backends belonging to the
        same role - which is what actually happens in this kind of incident, and
        which forces every service to reconnect into a server with no room left.
        """
        # Take what is free, evict the rest, then immediately take the slots the
        # eviction just freed. Without that second pass the scenario cancels
        # itself out: terminating three service backends frees exactly three
        # slots, and the services reconnect straight back into them.
        opened = await self._open_up_to(count, dsn)
        evicted = await self._evict_other_backends()
        if evicted:
            opened += await self._open_up_to(evicted, dsn)

        logger.warning(
            "chaos_db_connections_held",
            extra={
                "chaos_scenario": "db_exhaustion",
                "opened": opened,
                "total_held": len(self._held_connections),
                "evicted_backends": evicted,
            },
        )
        return opened

    async def _open_up_to(self, count: int, dsn: str) -> int:
        """Open up to ``count`` connections, stopping at the first refusal."""
        import asyncpg

        opened = 0
        for _ in range(max(0, count)):
            try:
                connection = await asyncpg.connect(dsn, timeout=3)
            except Exception as exc:  # noqa: BLE001 - refusal is the signal
                logger.error(
                    "chaos_db_connection_refused",
                    extra={
                        "chaos_scenario": "db_exhaustion",
                        "held": len(self._held_connections),
                        "error": str(exc)[:200],
                    },
                )
                break
            self._held_connections.append(connection)
            opened += 1
        return opened

    async def _evict_other_backends(self) -> int:
        """Terminate same-role backends that are not ones we hold.

        A role may terminate its own role's backends without being a superuser,
        so this needs no extra privileges. Returns how many were terminated.
        """
        if not self._held_connections:
            return 0

        executor = self._held_connections[0]
        try:
            our_pids = [connection.get_server_pid() for connection in self._held_connections]
            rows = await executor.fetch(
                """
                SELECT pg_terminate_backend(pid) AS terminated
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND usename = current_user
                  AND pid <> pg_backend_pid()
                  AND NOT (pid = ANY($1::int[]))
                """,
                our_pids,
            )
        except Exception as exc:  # noqa: BLE001 - best effort by design
            logger.warning(
                "chaos_db_eviction_failed",
                extra={"chaos_scenario": "db_exhaustion", "error": str(exc)[:200]},
            )
            return 0
        return sum(1 for row in rows if row["terminated"])

    async def release_connections(self) -> int:
        """Close every held connection. Returns how many were released."""
        released = 0
        for connection in self._held_connections:
            with contextlib.suppress(Exception):
                await connection.close()
            released += 1
        self._held_connections.clear()
        if released:
            logger.warning(
                "chaos_db_connections_released",
                extra={"chaos_scenario": "db_exhaustion", "released": released},
            )
        return released

    @property
    def held_connections(self) -> int:
        return len(self._held_connections)

    # --- lifecycle --------------------------------------------------------
    async def reset(self) -> None:
        """Return the process to a healthy state."""
        self.set_latency(0)
        await self.stop_memory_leak(free=True)
        await self.release_connections()
        logger.info("chaos_reset", extra={"service": self.service_name})

    def snapshot(self) -> dict[str, Any]:
        """Reported by ``GET /_chaos`` and surfaced in the console."""
        return {
            "service": self.service_name,
            "latency_ms": self.latency_ms,
            "memory_leak_active": self.memory_leak_active,
            "leaked_mb": self.leaked_mb,
            "held_db_connections": self.held_connections,
            "healthy": (
                self.latency_ms == 0
                and not self.memory_leak_active
                and self.leaked_mb == 0
                and self.held_connections == 0
            ),
        }
