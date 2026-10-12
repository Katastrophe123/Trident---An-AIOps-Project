"""Endpoint behaviour for the three services.

The theme throughout: **a refusal is not an outage**. Most of these tests exist
to pin down that distinction, because the agent in Phase 2 has to make the same
call and will be scored on it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from services.common.app import ServiceContext
from services.orders.main import DEFAULT_UNIT_PRICE, UNIT_PRICES, unit_price


class TestPayments:
    async def test_captures_a_payment(self, payments_client: AsyncClient) -> None:
        response = await payments_client.post(
            "/payments", json={"order_id": str(uuid.uuid4()), "amount": 1199.00}
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "captured"
        assert body["amount"] == 1199.00
        assert uuid.UUID(body["id"])

    async def test_declines_above_the_threshold_with_402_not_500(
        self, payments_client: AsyncClient
    ) -> None:
        """A decline is a business outcome. A 5xx would make the agent treat
        an ordinary refusal as an incident."""
        response = await payments_client.post(
            "/payments", json={"order_id": str(uuid.uuid4()), "amount": 25_000.00}
        )

        assert response.status_code == 402
        assert "declined" in response.json()["error"]["message"]

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"order_id": "not-a-uuid", "amount": 10},
            {"order_id": str(uuid.uuid4()), "amount": -5},
            {"order_id": str(uuid.uuid4()), "amount": 0},
        ],
    )
    async def test_invalid_payloads_return_422(
        self, payments_client: AsyncClient, payload: dict[str, Any]
    ) -> None:
        response = await payments_client.post("/payments", json=payload)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    async def test_count_reflects_captured_payments(self, payments_client: AsyncClient) -> None:
        before = (await payments_client.get("/payments/count")).json()["payments"]
        await payments_client.post(
            "/payments", json={"order_id": str(uuid.uuid4()), "amount": 10.0}
        )
        after = (await payments_client.get("/payments/count")).json()["payments"]
        assert after == before + 1


class TestInventory:
    async def test_seeds_stock_on_startup(self, inventory_client: AsyncClient) -> None:
        stock = (await inventory_client.get("/stock")).json()
        assert len(stock) == 5
        assert all(item["available"] == 500 for item in stock)

    async def test_reserving_decrements_available_and_increments_reserved(
        self, inventory_client: AsyncClient
    ) -> None:
        response = await inventory_client.post(
            "/reservations",
            json={"order_id": str(uuid.uuid4()), "sku": "SKU-LAPTOP-13", "quantity": 3},
        )

        assert response.status_code == 201
        assert response.json()["available_after"] == 497

        item = (await inventory_client.get("/stock/SKU-LAPTOP-13")).json()
        assert item["available"] == 497
        assert item["reserved"] == 3

    async def test_insufficient_stock_is_409_not_500(self, inventory_client: AsyncClient) -> None:
        response = await inventory_client.post(
            "/reservations",
            json={"order_id": str(uuid.uuid4()), "sku": "SKU-LAPTOP-13", "quantity": 50},
        )
        # 50 > the per-request cap of 50? No - cap is 50, stock is 500, so this
        # succeeds; drain it first to make the shelf genuinely empty.
        assert response.status_code == 201

        for _ in range(10):
            await inventory_client.post(
                "/reservations",
                json={
                    "order_id": str(uuid.uuid4()),
                    "sku": "SKU-LAPTOP-13",
                    "quantity": 50,
                },
            )

        final = await inventory_client.post(
            "/reservations",
            json={"order_id": str(uuid.uuid4()), "sku": "SKU-LAPTOP-13", "quantity": 50},
        )
        assert final.status_code == 409
        assert "Insufficient stock" in final.json()["error"]["message"]

    async def test_unknown_sku_is_404(self, inventory_client: AsyncClient) -> None:
        response = await inventory_client.post(
            "/reservations",
            json={"order_id": str(uuid.uuid4()), "sku": "SKU-NOPE", "quantity": 1},
        )
        assert response.status_code == 404

    async def test_release_returns_stock_to_the_shelf(self, inventory_client: AsyncClient) -> None:
        order_id = str(uuid.uuid4())
        await inventory_client.post(
            "/reservations",
            json={"order_id": order_id, "sku": "SKU-MOUSE-WL", "quantity": 4},
        )

        release = await inventory_client.post(
            "/reservations/release",
            json={"order_id": order_id, "sku": "SKU-MOUSE-WL", "quantity": 4},
        )

        assert release.status_code == 200
        assert release.json()["available_after"] == 500
        item = (await inventory_client.get("/stock/SKU-MOUSE-WL")).json()
        assert item["reserved"] == 0

    async def test_releasing_an_unknown_reservation_does_not_raise(
        self, inventory_client: AsyncClient
    ) -> None:
        """Compensation must never fail - it runs on the error path already."""
        response = await inventory_client.post(
            "/reservations/release",
            json={"order_id": str(uuid.uuid4()), "sku": "SKU-MOUSE-WL", "quantity": 1},
        )
        assert response.status_code == 200
        assert response.json()["reason"] == "no matching reservation"

    async def test_restock_resets_to_seeded_levels(self, inventory_client: AsyncClient) -> None:
        await inventory_client.post(
            "/reservations",
            json={"order_id": str(uuid.uuid4()), "sku": "SKU-DOCK-USBC", "quantity": 10},
        )
        restocked = (await inventory_client.post("/stock/restock")).json()
        assert all(item["available"] == 500 and item["reserved"] == 0 for item in restocked)

    async def test_unknown_sku_stock_lookup_is_404(self, inventory_client: AsyncClient) -> None:
        assert (await inventory_client.get("/stock/SKU-NOPE")).status_code == 404


class TestOrders:
    async def test_a_good_order_is_confirmed(
        self, orders_client: AsyncClient, order_payload: dict[str, Any]
    ) -> None:
        response = await orders_client.post("/orders", json=order_payload)

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "confirmed"
        assert body["failure_reason"] is None
        assert body["amount"] == float(UNIT_PRICES["SKU-LAPTOP-13"])

    async def test_order_amount_is_price_times_quantity(self, orders_client: AsyncClient) -> None:
        response = await orders_client.post(
            "/orders",
            json={"customer_id": "c", "sku": "SKU-MOUSE-WL", "quantity": 3},
        )
        assert response.json()["amount"] == float(UNIT_PRICES["SKU-MOUSE-WL"] * 3)

    def test_unknown_sku_falls_back_to_a_default_price(self) -> None:
        assert unit_price("SKU-WHATEVER") == DEFAULT_UNIT_PRICE
        assert unit_price("SKU-LAPTOP-13") == UNIT_PRICES["SKU-LAPTOP-13"]

    async def test_an_unknown_sku_is_rejected_not_failed(self, orders_client: AsyncClient) -> None:
        """inventory returns 404, which is a refusal: the order is *rejected*
        and still a 201, because nothing is broken."""
        response = await orders_client.post(
            "/orders",
            json={"customer_id": "c", "sku": "SKU-DOES-NOT-EXIST", "quantity": 1},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "rejected"
        assert "inventory rejected" in body["failure_reason"]

    async def test_a_declined_payment_is_rejected_not_failed(
        self, orders_client: AsyncClient
    ) -> None:
        response = await orders_client.post(
            "/orders",
            json={"customer_id": "c", "sku": "SKU-LAPTOP-15", "quantity": 20},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "rejected"
        assert "payments rejected" in body["failure_reason"]

    async def test_a_declined_payment_releases_the_reservation(
        self, orders_client: AsyncClient, inventory_client: AsyncClient
    ) -> None:
        """Otherwise a long chaos run silently drains the shelf."""
        before = (await inventory_client.get("/stock/SKU-LAPTOP-15")).json()

        await orders_client.post(
            "/orders",
            json={"customer_id": "c", "sku": "SKU-LAPTOP-15", "quantity": 20},
        )

        after = (await inventory_client.get("/stock/SKU-LAPTOP-15")).json()
        assert after["available"] == before["available"]
        assert after["reserved"] == before["reserved"]

    async def test_listing_and_filtering_orders(
        self, orders_client: AsyncClient, order_payload: dict[str, Any]
    ) -> None:
        await orders_client.post("/orders", json=order_payload)
        await orders_client.post(
            "/orders", json={"customer_id": "c", "sku": "SKU-NOPE", "quantity": 1}
        )

        everything = (await orders_client.get("/orders")).json()
        confirmed = (await orders_client.get("/orders?status=confirmed")).json()

        assert len(everything) == 2
        assert len(confirmed) == 1
        assert confirmed[0]["status"] == "confirmed"

    async def test_summary_counts_by_status(
        self, orders_client: AsyncClient, order_payload: dict[str, Any]
    ) -> None:
        await orders_client.post("/orders", json=order_payload)
        await orders_client.post(
            "/orders", json={"customer_id": "c", "sku": "SKU-NOPE", "quantity": 1}
        )

        summary = (await orders_client.get("/orders/summary")).json()
        assert summary == {"confirmed": 1, "rejected": 1}

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"customer_id": "c"},
            {"customer_id": "", "sku": "SKU-LAPTOP-13", "quantity": 1},
            {"customer_id": "c", "sku": "SKU-LAPTOP-13", "quantity": 0},
            {"customer_id": "c", "sku": "SKU-LAPTOP-13", "quantity": 999},
        ],
    )
    async def test_invalid_payloads_return_422(
        self, orders_client: AsyncClient, payload: dict[str, Any]
    ) -> None:
        response = await orders_client.post("/orders", json=payload)
        assert response.status_code == 422

    async def test_health_reports_both_downstreams(self, orders_client: AsyncClient) -> None:
        body = (await orders_client.get("/health")).json()
        names = {dep["name"] for dep in body["dependencies"]}
        assert {"postgres", "payments", "inventory"} <= names
        assert body["status"] == "healthy"


class TestErrorEnvelope:
    async def test_every_error_carries_a_request_id(self, orders_client: AsyncClient) -> None:
        response = await orders_client.post("/orders", json={})
        assert response.status_code == 422
        assert response.json()["request_id"]

    async def test_the_request_id_header_is_echoed(self, orders_client: AsyncClient) -> None:
        response = await orders_client.get("/health", headers={"X-Request-ID": "trace-abc"})
        assert response.headers["X-Request-ID"] == "trace-abc"

    async def test_an_unknown_route_is_a_structured_404(self, orders_client: AsyncClient) -> None:
        response = await orders_client.get("/nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "http_error"


class TestServiceContext:
    def test_context_exposes_the_service_name(
        self, payments_app: tuple[Any, ServiceContext]
    ) -> None:
        _, context = payments_app
        assert context.service_name == "payments-service"


class TestReplenishment:
    """Without this the demo has a shelf life of about ten minutes.

    Nothing ever ships an order, so every confirmed order moves a unit from
    ``available`` to ``reserved`` permanently. Once the shelf is empty every
    order is a 409 and orders stops calling payments at all - so the payment
    scenarios lose their symptoms. This was caught by watching a stack that had
    been up for forty minutes, not by a test.
    """

    async def test_a_full_shelf_is_left_alone(
        self, inventory_app: tuple[Any, ServiceContext]
    ) -> None:
        from services.inventory.main import replenish_stock

        _, context = inventory_app
        assert await replenish_stock(context) == []

    async def test_a_drained_sku_is_restocked_to_its_seeded_level(
        self, inventory_app: tuple[Any, ServiceContext], inventory_client: AsyncClient
    ) -> None:
        from services.inventory.main import replenish_stock

        _, context = inventory_app
        # Drain one SKU the way traffic does: 10 reservations of 50.
        for _ in range(10):
            await inventory_client.post(
                "/reservations",
                json={
                    "order_id": str(uuid.uuid4()),
                    "sku": "SKU-LAPTOP-13",
                    "quantity": 50,
                },
            )
        drained = (await inventory_client.get("/stock/SKU-LAPTOP-13")).json()
        assert drained["available"] == 0
        assert drained["reserved"] == 500

        assert await replenish_stock(context) == ["SKU-LAPTOP-13"]

        restocked = (await inventory_client.get("/stock/SKU-LAPTOP-13")).json()
        assert restocked["available"] == 500
        # Reserved is cleared too: those units are treated as shipped, otherwise
        # the counter grows forever and means nothing.
        assert restocked["reserved"] == 0

    async def test_only_depleted_skus_are_touched(
        self, inventory_app: tuple[Any, ServiceContext], inventory_client: AsyncClient
    ) -> None:
        from services.inventory.main import replenish_stock

        _, context = inventory_app
        # 50 of 500 is 10%, below the 20% threshold.
        for _ in range(9):
            await inventory_client.post(
                "/reservations",
                json={"order_id": str(uuid.uuid4()), "sku": "SKU-MOUSE-WL", "quantity": 50},
            )
        # A single unit off a different SKU leaves it well above the threshold.
        await inventory_client.post(
            "/reservations",
            json={"order_id": str(uuid.uuid4()), "sku": "SKU-KEYB-MECH", "quantity": 1},
        )

        assert await replenish_stock(context) == ["SKU-MOUSE-WL"]
        assert (await inventory_client.get("/stock/SKU-KEYB-MECH")).json()["available"] == 499

    async def test_orders_keep_flowing_after_the_shelf_empties(
        self, orders_stack: dict[str, Any], orders_client: AsyncClient
    ) -> None:
        """The symptom that mattered: once drained, every order was rejected and
        payments saw no traffic at all.

        A cheap SKU on purpose - 50 laptops is over the payment decline
        threshold, and a declined payment *releases* the reservation, so the
        shelf would never actually empty.
        """
        from services.inventory.main import replenish_stock

        for _ in range(10):
            await orders_client.post(
                "/orders",
                json={"customer_id": "c", "sku": "SKU-MOUSE-WL", "quantity": 50},
            )
        rejected = await orders_client.post(
            "/orders", json={"customer_id": "c", "sku": "SKU-MOUSE-WL", "quantity": 1}
        )
        assert rejected.json()["status"] == "rejected"
        assert "inventory rejected" in rejected.json()["failure_reason"]

        await replenish_stock(orders_stack["inventory_context"])

        confirmed = await orders_client.post(
            "/orders", json={"customer_id": "c", "sku": "SKU-MOUSE-WL", "quantity": 1}
        )
        assert confirmed.json()["status"] == "confirmed"

    async def test_a_database_failure_is_logged_not_raised(
        self, inventory_app: tuple[Any, ServiceContext]
    ) -> None:
        """A failing database is a symptom of another scenario; this task must
        not turn it into a crashed background task."""
        from sqlalchemy.exc import OperationalError

        from services.inventory.main import replenish_stock

        _, context = inventory_app

        class FailingSession:
            async def __aenter__(self) -> Any:
                raise OperationalError("SELECT 1", {}, Exception("too many connections"))

            async def __aexit__(self, *args: Any) -> None:
                return None

        original = context.database.session
        context.database.session = lambda: FailingSession()  # type: ignore[method-assign]
        try:
            assert await replenish_stock(context) == []
        finally:
            context.database.session = original  # type: ignore[method-assign]

    async def test_a_missing_row_is_treated_as_depleted(
        self, inventory_app: tuple[Any, ServiceContext], inventory_client: AsyncClient
    ) -> None:
        """Recovery from a rebuilt database.

        `alembic downgrade base` followed by an upgrade leaves the table empty
        while the service is still running. Skipping absent rows meant every
        order 404'd until someone restocked by hand.
        """
        from sqlalchemy import delete

        from services.common.models import InventoryItem
        from services.inventory.main import replenish_stock

        _, context = inventory_app
        async with context.database.session() as session:
            await session.execute(delete(InventoryItem))

        assert (await inventory_client.get("/stock")).json() == []

        replenished = await replenish_stock(context)

        assert len(replenished) == 5
        stock = (await inventory_client.get("/stock")).json()
        assert len(stock) == 5
        assert all(item["available"] == 500 for item in stock)
