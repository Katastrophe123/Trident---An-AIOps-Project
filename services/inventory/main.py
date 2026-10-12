"""inventory-service: checks and reserves stock.

This is the service the "slow dependency" scenario targets. Nothing here knows
about that: the delay is injected by the shared middleware, which is why the
latency shows up attributed to *inventory* in orders' metrics rather than
looking like a generic slowdown.

It also seeds stock on startup, so the traffic generator always has something
to order.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Path, status
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from services.common.app import ServiceContext, build_app
from services.common.config import get_settings
from services.common.logging_setup import get_logger
from services.common.models import SEED_SKUS, InventoryItem, Reservation
from services.common.schemas import (
    ReservationRequest,
    ReservationResponse,
    StockResponse,
)

logger = get_logger(__name__)

#: How often the replenishment task runs.
REPLENISH_INTERVAL_SECONDS = 20.0
#: Restock a SKU once it drops to this fraction of its seeded level.
REPLENISH_THRESHOLD = 0.2


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        context: ServiceContext = app.state.context
        await seed_stock(context)
        replenisher = asyncio.create_task(replenish_forever(context))
        logger.info(
            "service_started",
            extra={
                "service": context.service_name,
                "environment": settings.environment,
                "chaos_enabled": settings.chaos_enabled,
            },
        )
        try:
            yield
        finally:
            replenisher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await replenisher
            await context.database.dispose()
            logger.info("service_stopped", extra={"service": context.service_name})

    app, context = build_app(
        settings=settings,
        title="inventory-service",
        description="Checks and reserves stock for the AIOps demo system.",
    )
    app.router.lifespan_context = lifespan
    register_routes(app, context)
    return app


async def seed_stock(context: ServiceContext) -> None:
    """Insert the demo SKUs if they are absent.

    Tolerant of a failing database on purpose: the service must still start so
    that ``/health`` can *report* the failure. A service that refuses to boot
    when its database is misconfigured gives the agent nothing to read, which
    is the whole point of the "bad config" scenario.
    """
    try:
        async with context.database.session() as session:
            existing = set((await session.scalars(select(InventoryItem.sku))).all())
            added = 0
            for sku, quantity in SEED_SKUS:
                if sku not in existing:
                    session.add(InventoryItem(sku=sku, available=quantity, reserved=0))
                    added += 1
        if added:
            logger.info("stock_seeded", extra={"skus_added": added})
    except SQLAlchemyError as exc:
        logger.error(
            "stock_seed_failed",
            extra={"error": f"{type(exc).__name__}: {exc}"[:400]},
        )


async def replenish_stock(context: ServiceContext) -> list[str]:
    """Top any depleted SKU back up to its seeded level.

    Without this the demo has a shelf life of about ten minutes. Nothing here
    ever *ships* an order, so every confirmed order moves a unit from
    ``available`` to ``reserved`` permanently; 2500 seeded units at roughly four
    units a second drains the warehouse, after which every order is a 409 and
    **orders stops calling payments altogether**. Three of the five scenarios
    then have nothing to show.

    Restocking also clears that SKU's reservation rows, treating them as shipped.
    The release path already tolerates a missing reservation, so this is safe
    while orders are in flight.

    Returns the SKUs that were replenished.
    """
    replenished: list[str] = []
    try:
        async with context.database.session() as session:
            for sku, quantity in SEED_SKUS:
                item = await session.get(InventoryItem, sku, with_for_update=True)
                if item is None:
                    # A missing row counts as depleted, not as "nothing to do".
                    # Otherwise the service never recovers from a database that
                    # was rebuilt after startup - an `alembic downgrade base`
                    # followed by an upgrade leaves the table empty, every order
                    # 404s, and only a manual restock fixes it.
                    session.add(InventoryItem(sku=sku, available=quantity, reserved=0))
                    replenished.append(sku)
                    continue
                if item.available > quantity * REPLENISH_THRESHOLD:
                    continue
                await session.execute(delete(Reservation).where(Reservation.sku == sku))
                item.available = quantity
                item.reserved = 0
                replenished.append(sku)
    except SQLAlchemyError as exc:
        # A failing database here is a symptom of another scenario, not of this
        # task. Log it and let /health be the thing that reports it.
        logger.warning(
            "stock_replenish_failed",
            extra={"error": f"{type(exc).__name__}: {exc}"[:200]},
        )
        return []

    if replenished:
        logger.info(
            "stock_replenished",
            extra={"skus": replenished, "restocked_to": SEED_SKUS[0][1]},
        )
    return replenished


async def replenish_forever(context: ServiceContext) -> None:
    """Run :func:`replenish_stock` on a loop for as long as the service lives."""
    while True:
        await asyncio.sleep(REPLENISH_INTERVAL_SECONDS)
        await replenish_stock(context)


def register_routes(app: FastAPI, context: ServiceContext) -> None:
    @app.post(
        "/reservations",
        response_model=ReservationResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["inventory"],
        summary="Reserve stock for an order",
        responses={409: {"description": "Insufficient stock"}, 404: {"description": "Unknown SKU"}},
    )
    async def reserve(payload: ReservationRequest) -> ReservationResponse:
        """Reserve ``quantity`` of ``sku``.

        Out of stock is a **409** and an unknown SKU a **404** — business
        outcomes, not incidents.
        """
        async with context.database.session() as session:
            item = await session.get(InventoryItem, payload.sku, with_for_update=True)
            if item is None:
                logger.warning("reservation_unknown_sku", extra={"sku": payload.sku})
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Unknown SKU: {payload.sku}",
                )
            if item.available < payload.quantity:
                logger.warning(
                    "reservation_insufficient_stock",
                    extra={
                        "sku": payload.sku,
                        "requested": payload.quantity,
                        "available": item.available,
                    },
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Insufficient stock for {payload.sku}: "
                        f"requested {payload.quantity}, available {item.available}"
                    ),
                )

            item.available -= payload.quantity
            item.reserved += payload.quantity
            session.add(
                Reservation(
                    order_id=payload.order_id,
                    sku=payload.sku,
                    quantity=payload.quantity,
                )
            )
            available_after = item.available

        logger.info(
            "stock_reserved",
            extra={
                "order_id": str(payload.order_id),
                "sku": payload.sku,
                "quantity": payload.quantity,
                "available_after": available_after,
            },
        )
        return ReservationResponse(
            reserved=True,
            sku=payload.sku,
            quantity=payload.quantity,
            available_after=available_after,
        )

    @app.post(
        "/reservations/release",
        response_model=ReservationResponse,
        tags=["inventory"],
        summary="Release a reservation",
    )
    async def release(payload: ReservationRequest) -> ReservationResponse:
        """Return reserved stock to the shelf.

        Called by orders when payment fails after a successful reservation, so
        a long chaos run does not silently drain stock. Idempotent-ish: if the
        reservation is already gone the stock is left alone and ``reserved`` is
        reported false rather than raising, because compensation must not fail.
        """
        async with context.database.session() as session:
            order_id = payload.order_id
            reservation = (
                await session.scalars(
                    select(Reservation).where(
                        Reservation.order_id == order_id,
                        Reservation.sku == payload.sku,
                    )
                )
            ).first()
            item = await session.get(InventoryItem, payload.sku, with_for_update=True)

            if reservation is None or item is None:
                logger.info(
                    "reservation_release_noop",
                    extra={"order_id": str(order_id), "sku": payload.sku},
                )
                return ReservationResponse(
                    reserved=False,
                    sku=payload.sku,
                    quantity=0,
                    available_after=item.available if item else 0,
                    reason="no matching reservation",
                )

            quantity = reservation.quantity
            item.available += quantity
            item.reserved = max(0, item.reserved - quantity)
            await session.delete(reservation)
            available_after = item.available

        logger.info(
            "reservation_released",
            extra={
                "order_id": str(payload.order_id),
                "sku": payload.sku,
                "quantity": quantity,
                "available_after": available_after,
            },
        )
        return ReservationResponse(
            reserved=False,
            sku=payload.sku,
            quantity=quantity,
            available_after=available_after,
            reason="released",
        )

    @app.get(
        "/stock/{sku}",
        response_model=StockResponse,
        tags=["inventory"],
        summary="Current stock for a SKU",
        responses={404: {"description": "Unknown SKU"}},
    )
    async def stock(sku: str = Path(max_length=64)) -> StockResponse:
        async with context.database.session() as session:
            item = await session.get(InventoryItem, sku)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown SKU: {sku}")
        return StockResponse.model_validate(item, from_attributes=True)

    @app.get(
        "/stock",
        response_model=list[StockResponse],
        tags=["inventory"],
        summary="Stock for every SKU",
    )
    async def all_stock() -> list[StockResponse]:
        async with context.database.session() as session:
            items = (await session.scalars(select(InventoryItem).order_by(InventoryItem.sku))).all()
        return [StockResponse.model_validate(item, from_attributes=True) for item in items]

    @app.post(
        "/stock/restock",
        response_model=list[StockResponse],
        tags=["inventory"],
        summary="Reset stock to its seeded levels",
    )
    async def restock() -> list[StockResponse]:
        """Used by ``chaos.py reset`` so long demo runs cannot drain stock."""
        async with context.database.session() as session:
            for sku, quantity in SEED_SKUS:
                item = await session.get(InventoryItem, sku)
                if item is None:
                    session.add(InventoryItem(sku=sku, available=quantity, reserved=0))
                else:
                    item.available = quantity
                    item.reserved = 0
            await session.flush()
            items = (await session.scalars(select(InventoryItem).order_by(InventoryItem.sku))).all()
            payload = [StockResponse.model_validate(item, from_attributes=True) for item in items]
        logger.info("stock_restocked", extra={"skus": len(payload)})
        return payload


app = create_app()
