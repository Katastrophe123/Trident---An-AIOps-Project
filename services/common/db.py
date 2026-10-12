"""Async engine and session management, shared by all three services.

The pool is deliberately tiny (``db_pool_size=3``, overflow 2) and Postgres is
started with ``max_connections=25``. Together that makes the "database
exhaustion" scenario reachable in seconds rather than needing hundreds of
connections — and it means an exhausted pool shows up as a *timeout*, which is
the symptom the agent has to recognise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from services.common.config import ServiceSettings
from services.common.logging_setup import get_logger

logger = get_logger(__name__)


class Database:
    """Owns the engine and session factory for the process lifetime."""

    def __init__(self, url: str, *, echo: bool = False, **engine_kwargs: Any) -> None:
        self._url = url
        kwargs: dict[str, Any] = {"echo": echo, "future": True, **engine_kwargs}
        if url.startswith("sqlite"):
            # One shared in-memory connection so tests see the same data.
            kwargs.setdefault("poolclass", StaticPool)
            kwargs.setdefault("connect_args", {"check_same_thread": False})
            for key in (
                "pool_size",
                "max_overflow",
                "pool_timeout",
                "pool_pre_ping",
                "pool_recycle",
            ):
                kwargs.pop(key, None)
        self._engine: AsyncEngine = create_async_engine(url, **kwargs)
        self._session_factory = async_sessionmaker(
            bind=self._engine, expire_on_commit=False, autoflush=False
        )

    @classmethod
    def from_settings(cls, settings: ServiceSettings) -> Database:
        return cls(
            settings.database_url,
            echo=settings.db_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_pre_ping=True,
            pool_recycle=settings.db_pool_recycle_seconds,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def url(self) -> str:
        return self._url

    @property
    def asyncpg_dsn(self) -> str:
        """The URL in the form raw asyncpg wants (no SQLAlchemy driver prefix)."""
        return self._url.replace("postgresql+asyncpg://", "postgresql://")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional scope: commit on success, roll back on error."""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def healthcheck(self) -> tuple[bool, str | None]:
        """Return ``(ok, error_message)``.

        The error string is returned rather than swallowed because it is the
        evidence that distinguishes the chaos scenarios: a wrong password gives
        "password authentication failed", exhaustion gives "too many
        connections" or a pool timeout.
        """
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - health must not raise
            message = f"{type(exc).__name__}: {exc}"
            logger.error("database_healthcheck_failed", extra={"error": message[:500]})
            return False, message[:500]
        return True, None

    async def dispose(self) -> None:
        await self._engine.dispose()
