"""Structured JSON logging, plus an in-memory buffer of recent records.

Two consumers:

* **stdout** — one JSON object per line, which is what ``docker compose logs``
  shows and what a real log shipper would ingest.
* **an in-memory ring buffer** — exposed by ``GET /_logs``, so the console (and
  from Phase 2, the agent's ``get_logs`` tool) can read recent errors without
  shelling out to Docker.

Deliberately built on stdlib ``logging`` with a ~40-line formatter rather than a
logging framework: the whole pipeline is readable in one sitting.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import deque
from contextvars import ContextVar
from datetime import UTC, datetime
from threading import Lock
from typing import Any

#: Set per request by the middleware so every log line in a request correlates.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

#: Attributes ``logging`` puts on every record; anything else is caller context.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class LogBuffer:
    """Thread-safe ring buffer of the most recent log records.

    Bounded by construction, so a chatty service cannot leak memory through it -
    which matters here, because one of the chaos scenarios *is* a memory leak
    and it must not be confused with this.
    """

    def __init__(self, maxlen: int = 500) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = Lock()

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._records.append(record)

    def tail(
        self,
        *,
        limit: int = 100,
        level: str | None = None,
        since_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        """Most recent records first, optionally filtered by level and age."""
        with self._lock:
            records = list(self._records)

        if level:
            wanted = level.upper()
            threshold = logging.getLevelNamesMapping().get(wanted, logging.INFO)
            records = [
                item
                for item in records
                if logging.getLevelNamesMapping().get(item.get("level", "INFO"), 0) >= threshold
            ]
        if since_seconds is not None:
            cutoff = time.time() - since_seconds
            records = [item for item in records if item.get("epoch", 0) >= cutoff]

        return list(reversed(records))[:limit]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class JsonFormatter(logging.Formatter):
    """Renders a log record as a single JSON object."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def build(self, record: logging.LogRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "epoch": record.created,
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Anything passed via logger.info("x", extra={...}) becomes a field.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return payload

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.build(record), default=str)


class BufferHandler(logging.Handler):
    """Mirrors every formatted record into a :class:`LogBuffer`."""

    def __init__(self, buffer: LogBuffer, formatter: JsonFormatter) -> None:
        super().__init__()
        self._buffer = buffer
        self._json_formatter = formatter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(self._json_formatter.build(record))
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


#: Loggers pinned to WARNING - see configure_logging.
#:
#: ``uvicorn.access`` is here for a different reason than the others: it is not
#: third-party noise, it is a *duplicate*. It emits one unstructured line per
#: request (`'GET /orders HTTP/1.1' 200`) with no request id, while the
#: middleware already emits `request_completed` with method, path, status and
#: latency as real fields. Keeping both halved the useful capacity of the ring
#: buffer and gave the agent two records per request, one of them uncorrelatable.
NOISY_LIBRARIES = (
    "httpx",
    "httpcore",
    "aiosqlite",
    "asyncio",
    "uvicorn.access",
)


def configure_logging(
    *, service_name: str, level: str = "INFO", buffer_size: int = 500
) -> LogBuffer:
    """Install the JSON formatter on stdout plus the ring buffer.

    Returns the buffer so the app can expose it on ``GET /_logs``.
    """
    formatter = JsonFormatter(service_name)
    buffer = LogBuffer(maxlen=buffer_size)

    stream = logging.StreamHandler(stream=sys.stdout)
    stream.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(stream)
    root.addHandler(BufferHandler(buffer, formatter))
    root.setLevel(logging.getLevelNamesMapping()[level.upper()])

    # uvicorn installs its own handlers; drop them so output stays one format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # Libraries that log one unstructured INFO line per call. Our own
    # `request_completed` already carries the method, path, status and latency
    # as fields, so these are pure duplication - and at a few orders a second
    # they would crowd the real events out of the ring buffer the agent reads.
    # WARNING, not silence: a genuine connection failure still gets through.
    for name in NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)

    return buffer


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
