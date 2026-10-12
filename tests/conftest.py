"""Shared fixtures.

Everything here runs offline: SQLite in-memory for the database, and the three
services mounted as ASGI apps wired to each other with ``httpx`` transports.
No Docker, no PostgreSQL, no network.

One wrinkle worth knowing: ``configure_logging`` installs handlers on the
**root** logger, so when several services are built inside one test process the
last one wins and its name appears on every log line. In production each service
is its own process, so this only affects tests - which is why no test asserts on
the ``service`` field of a log record written by a different app.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.common.app import ServiceContext, build_app
from services.common.config import ServiceSettings
from services.common.db import Database
from services.common.models import Base

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def make_settings(service_name: str, **overrides: Any) -> ServiceSettings:
    """Settings for one service under test."""
    values: dict[str, Any] = {
        "service_name": service_name,
        "environment": "test",
        # INFO, not DEBUG: aiosqlite logs every statement at DEBUG.
        "log_level": "INFO",
        "database_url": TEST_DATABASE_URL,
        "chaos_enabled": True,
        "log_buffer_size": 200,
    }
    values.update(overrides)
    return ServiceSettings(**values)


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    """One in-memory database shared by every service in a test.

    Mirrors production, where the three services share one PostgreSQL.
    """
    db = Database(TEST_DATABASE_URL)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


# --------------------------------------------------------------------------- #
# payments
# --------------------------------------------------------------------------- #
@pytest.fixture
def payments_app(database: Database) -> Iterator[tuple[FastAPI, ServiceContext]]:
    from services.payments.main import register_routes

    app, context = build_app(
        settings=make_settings("payments-service"),
        title="payments-service",
        description="",
        database=database,
    )
    register_routes(app, context)
    yield app, context


@pytest.fixture
async def payments_client(
    payments_app: tuple[FastAPI, ServiceContext],
) -> AsyncIterator[AsyncClient]:
    app, _ = payments_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://payments") as client:
        yield client


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #
@pytest.fixture
async def inventory_app(
    database: Database,
) -> AsyncIterator[tuple[FastAPI, ServiceContext]]:
    from services.inventory.main import register_routes, seed_stock

    app, context = build_app(
        settings=make_settings("inventory-service"),
        title="inventory-service",
        description="",
        database=database,
    )
    register_routes(app, context)
    await seed_stock(context)
    yield app, context


@pytest.fixture
async def inventory_client(
    inventory_app: tuple[FastAPI, ServiceContext],
) -> AsyncIterator[AsyncClient]:
    app, _ = inventory_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://inventory") as client:
        yield client


# --------------------------------------------------------------------------- #
# orders, wired to the other two in-process
# --------------------------------------------------------------------------- #
@pytest.fixture
async def orders_stack(
    database: Database,
    payments_app: tuple[FastAPI, ServiceContext],
    inventory_app: tuple[FastAPI, ServiceContext],
) -> AsyncIterator[dict[str, Any]]:
    """orders with real (in-process) payments and inventory behind it.

    The downstream clients are given ASGI transports pointed at the other two
    apps, so ``orders -> inventory -> database`` is exercised end to end without
    a network.
    """
    from services.common.http import ServiceClient
    from services.orders.main import Downstreams, register_routes

    payments, payments_context = payments_app
    inventory, inventory_context = inventory_app

    app, context = build_app(
        settings=make_settings("orders-service"),
        title="orders-service",
        description="",
        database=database,
    )

    downstreams = Downstreams(
        payments=ServiceClient(
            name="payments",
            base_url="http://payments",
            timeout=30.0,
            metrics=context.metrics,
            client=AsyncClient(
                transport=ASGITransport(app=payments), base_url="http://payments", timeout=30.0
            ),
        ),
        inventory=ServiceClient(
            name="inventory",
            base_url="http://inventory",
            timeout=30.0,
            metrics=context.metrics,
            client=AsyncClient(
                transport=ASGITransport(app=inventory), base_url="http://inventory", timeout=30.0
            ),
        ),
    )

    async def check(client: ServiceClient) -> dict[str, Any]:
        ok, latency_ms, error = await client.health()
        return {
            "name": client.name,
            "status": "healthy" if ok else "unhealthy",
            "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
            "error": error,
        }

    context.extra_health_checks.extend(
        [lambda: check(downstreams.payments), lambda: check(downstreams.inventory)]
    )
    register_routes(app, context, downstreams)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://orders", timeout=30.0
    ) as client:
        yield {
            "client": client,
            "app": app,
            "context": context,
            "downstreams": downstreams,
            "payments_context": payments_context,
            "inventory_context": inventory_context,
        }

    await downstreams.aclose()


@pytest.fixture
def orders_client(orders_stack: dict[str, Any]) -> AsyncClient:
    return orders_stack["client"]


@pytest.fixture
def order_payload() -> dict[str, Any]:
    return {"customer_id": "cust-0001", "sku": "SKU-LAPTOP-13", "quantity": 1}
