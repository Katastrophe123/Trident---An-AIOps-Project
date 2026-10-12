"""orders-service: creates orders by calling payments and inventory.

The only service with downstream dependencies, which makes it the one that
*observes* most incidents. Two behaviours matter for diagnosis:

* **It distinguishes a refusal from an outage.** A declined payment or empty
  shelf is a ``rejected`` order and HTTP 201 — a business outcome. A dependency
  that cannot be reached is a ``failed`` order and HTTP 502, with the offending
  service named in both the response and the log line.
* **It reports dependency health on its own ``/health``.** So when payments is
  stopped, orders goes ``degraded`` with the reason attached rather than simply
  looking broken.

Reservation is attempted before payment, and released if payment then fails, so
the demo does not silently leak stock during a long chaos run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal

from fastapi import FastAPI, Query, status
from sqlalchemy import Select, func, select
from starlette.exceptions import HTTPException

from services.common.app import ServiceContext, build_app
from services.common.config import get_settings
from services.common.http import DownstreamError, ServiceClient
from services.common.logging_setup import get_logger
from services.common.models import Order
from services.common.schemas import DependencyHealth, OrderRequest, OrderResponse

logger = get_logger(__name__)

#: Fake unit prices, so an order has an amount without needing a catalogue service.
UNIT_PRICES: dict[str, Decimal] = {
    "SKU-LAPTOP-13": Decimal("1199.00"),
    "SKU-LAPTOP-15": Decimal("1499.00"),
    "SKU-DOCK-USBC": Decimal("189.00"),
    "SKU-MOUSE-WL": Decimal("39.00"),
    "SKU-KEYB-MECH": Decimal("109.00"),
}
DEFAULT_UNIT_PRICE = Decimal("99.00")


@dataclass
class Downstreams:
    """The clients orders needs. Held on the context so routes can reach them."""

    payments: ServiceClient
    inventory: ServiceClient

    async def aclose(self) -> None:
        await self.payments.aclose()
        await self.inventory.aclose()


def unit_price(sku: str) -> Decimal:
    return UNIT_PRICES.get(sku, DEFAULT_UNIT_PRICE)


def create_app() -> FastAPI:
    settings = get_settings()

    app, context = build_app(
        settings=settings,
        title="orders-service",
        description=(
            "Creates orders by calling payments-service and inventory-service. "
            "The entry point for the AIOps demo system."
        ),
    )

    downstreams = Downstreams(
        payments=ServiceClient(
            name="payments",
            base_url=settings.payments_url,
            timeout=settings.downstream_timeout_seconds,
            metrics=context.metrics,
        ),
        inventory=ServiceClient(
            name="inventory",
            base_url=settings.inventory_url,
            timeout=settings.downstream_timeout_seconds,
            metrics=context.metrics,
        ),
    )
    app.state.downstreams = downstreams

    # Surface downstream health on this service's own /health.
    async def check(client: ServiceClient) -> dict[str, object]:
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

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "service_started",
            extra={
                "service": context.service_name,
                "environment": settings.environment,
                "payments_url": settings.payments_url,
                "inventory_url": settings.inventory_url,
            },
        )
        try:
            yield
        finally:
            await downstreams.aclose()
            await context.database.dispose()
            logger.info("service_stopped", extra={"service": context.service_name})

    app.router.lifespan_context = lifespan
    register_routes(app, context, downstreams)
    return app


def register_routes(app: FastAPI, context: ServiceContext, downstreams: Downstreams) -> None:
    @app.post(
        "/orders",
        response_model=OrderResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["orders"],
        summary="Create an order",
        responses={502: {"description": "A downstream dependency is unavailable"}},
    )
    async def create_order(payload: OrderRequest) -> OrderResponse:
        """Reserve stock, take payment, record the order.

        Returns **201** for both confirmed and rejected orders (a rejection is
        a business outcome) and **502** when a dependency is unreachable.
        """
        order_id = uuid.uuid4()
        amount = (unit_price(payload.sku) * payload.quantity).quantize(Decimal("0.01"))

        # --- 1. reserve stock ---------------------------------------------
        try:
            await downstreams.inventory.post(
                "/reservations",
                {
                    "order_id": str(order_id),
                    "sku": payload.sku,
                    "quantity": payload.quantity,
                },
            )
        except DownstreamError as exc:
            return await _finish(
                context,
                order_id,
                payload,
                amount,
                exc,
                stage="reserve_stock",
            )

        # --- 2. take payment ----------------------------------------------
        try:
            await downstreams.payments.post(
                "/payments", {"order_id": str(order_id), "amount": float(amount)}
            )
        except DownstreamError as exc:
            # Release the reservation so stock is not leaked by a failed payment.
            await _release_stock(downstreams, order_id, payload)
            return await _finish(context, order_id, payload, amount, exc, stage="take_payment")

        # --- 3. record the confirmed order --------------------------------
        async with context.database.session() as session:
            order = Order(
                id=order_id,
                customer_id=payload.customer_id,
                sku=payload.sku,
                quantity=payload.quantity,
                amount=amount,
                status="confirmed",
            )
            session.add(order)
            await session.flush()
            response = OrderResponse.model_validate(order, from_attributes=True)

        logger.info(
            "order_confirmed",
            extra={
                "order_id": str(order_id),
                "customer_id": payload.customer_id,
                "sku": payload.sku,
                "quantity": payload.quantity,
                "amount": float(amount),
            },
        )
        return response

    @app.get(
        "/orders",
        response_model=list[OrderResponse],
        tags=["orders"],
        summary="Recent orders, newest first",
    )
    async def list_orders(
        limit: int = Query(default=20, ge=1, le=200),
        order_status: str | None = Query(default=None, alias="status", max_length=16),
    ) -> list[OrderResponse]:
        statement = select(Order).order_by(Order.created_at.desc()).limit(limit)
        if order_status:
            statement = statement.where(Order.status == order_status)
        async with context.database.session() as session:
            orders = (await session.scalars(statement)).all()
        return [OrderResponse.model_validate(order, from_attributes=True) for order in orders]

    @app.get("/orders/summary", tags=["orders"], summary="Order counts by status")
    async def summary() -> dict[str, int]:
        """What the console charts, and the quickest check that traffic is flowing."""
        async with context.database.session() as session:
            rows = (await session.execute(_status_counts())).all()
        return {str(row[0]): int(row[1]) for row in rows}


# SQLAlchemy 2.1 made Select variadic: Select[str, int], not
# Select[tuple[str, int]] as in 2.0.
def _status_counts() -> Select[str, int]:
    """Order counts grouped by status."""
    return select(Order.status, func.count(Order.id)).group_by(Order.status)


async def _release_stock(
    downstreams: Downstreams, order_id: uuid.UUID, payload: OrderRequest
) -> None:
    """Best-effort compensation; a failure here is logged, never raised."""
    try:
        await downstreams.inventory.post(
            "/reservations/release",
            {
                "order_id": str(order_id),
                "sku": payload.sku,
                "quantity": payload.quantity,
            },
        )
    except DownstreamError as exc:
        logger.warning(
            "stock_release_failed",
            extra={
                "order_id": str(order_id),
                "sku": payload.sku,
                "failure_kind": exc.kind,
            },
        )


async def _finish(
    context: ServiceContext,
    order_id: uuid.UUID,
    payload: OrderRequest,
    amount: Decimal,
    exc: DownstreamError,
    *,
    stage: str,
) -> OrderResponse:
    """Record a non-confirmed order, then return it or raise 502.

    The distinction is the useful part: a refusal is persisted as ``rejected``
    and returned as 201, while an outage is persisted as ``failed`` and raised
    as a 502 so it shows up in the error metrics the agent reads.
    """
    outage = exc.is_dependency_down
    reason = f"{exc.service} {exc.kind}: {exc.message}"[:300]

    try:
        async with context.database.session() as session:
            order = Order(
                id=order_id,
                customer_id=payload.customer_id,
                sku=payload.sku,
                quantity=payload.quantity,
                amount=amount,
                status="failed" if outage else "rejected",
                failure_reason=reason,
            )
            session.add(order)
            await session.flush()
            response = OrderResponse.model_validate(order, from_attributes=True)
    except Exception as db_exc:  # noqa: BLE001 - the database may also be the fault
        logger.error(
            "order_persist_failed",
            extra={
                "order_id": str(order_id),
                "stage": stage,
                "downstream": exc.service,
                "failure_kind": exc.kind,
                "db_error": f"{type(db_exc).__name__}: {db_exc}"[:300],
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{reason} (and the order could not be recorded)",
        ) from exc

    if outage:
        logger.error(
            "order_failed",
            extra={
                "order_id": str(order_id),
                "stage": stage,
                "downstream": exc.service,
                "failure_kind": exc.kind,
                "reason": reason,
            },
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=reason)

    logger.warning(
        "order_rejected",
        extra={
            "order_id": str(order_id),
            "stage": stage,
            "downstream": exc.service,
            "status_code": exc.status_code,
            "reason": reason,
        },
    )
    return response


# Re-exported so tests can assert on the shape without importing starlette.
__all__ = ["DependencyHealth", "create_app", "unit_price"]

app = create_app()
