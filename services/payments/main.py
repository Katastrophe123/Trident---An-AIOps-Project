"""payments-service: processes fake payments.

The simplest of the three — one dependency (the database) and no downstream
calls. That makes it the right target for the "crashed service" and "bad
config" scenarios: when it breaks, the cause is unambiguous, and the interesting
part is how the *caller* (orders) reports it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI, status
from sqlalchemy import func, select
from starlette.exceptions import HTTPException

from services.common.app import ServiceContext, build_app
from services.common.config import get_settings
from services.common.logging_setup import get_logger
from services.common.models import Payment
from services.common.schemas import PaymentRequest, PaymentResponse

logger = get_logger(__name__)

#: Payments above this are declined, so the demo has a realistic 4xx path that
#: is *not* an outage. Keeping the two distinguishable is the point.
DECLINE_THRESHOLD = Decimal("9000.00")


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        context: ServiceContext = app.state.context
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
            await context.database.dispose()
            logger.info("service_stopped", extra={"service": context.service_name})

    app, context = build_app(
        settings=settings,
        title="payments-service",
        description="Processes fake payments for the AIOps demo system.",
    )
    app.router.lifespan_context = lifespan
    register_routes(app, context)
    return app


def register_routes(app: FastAPI, context: ServiceContext) -> None:
    @app.post(
        "/payments",
        response_model=PaymentResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["payments"],
        summary="Capture a payment for an order",
        responses={402: {"description": "Payment declined"}},
    )
    async def create_payment(payload: PaymentRequest) -> PaymentResponse:
        """Capture (or decline) a payment.

        A decline is a **402**, not a 500: it is a business outcome, and the
        agent must not diagnose it as an incident.
        """
        amount = Decimal(str(payload.amount)).quantize(Decimal("0.01"))
        declined = amount > DECLINE_THRESHOLD

        async with context.database.session() as session:
            payment = Payment(
                order_id=payload.order_id,
                amount=amount,
                status="declined" if declined else "captured",
            )
            session.add(payment)
            await session.flush()
            response = PaymentResponse.model_validate(payment, from_attributes=True)

        if declined:
            logger.warning(
                "payment_declined",
                extra={
                    "order_id": str(payload.order_id),
                    "amount": float(amount),
                    "reason": "amount_above_threshold",
                },
            )
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Payment of {amount} declined: above the {DECLINE_THRESHOLD} limit",
            )

        logger.info(
            "payment_captured",
            extra={
                "payment_id": str(response.id),
                "order_id": str(payload.order_id),
                "amount": float(amount),
            },
        )
        return response

    @app.get("/payments/count", tags=["payments"], summary="How many payments exist")
    async def payment_count() -> dict[str, int]:
        """Cheap read that touches the database, useful for verifying recovery."""
        async with context.database.session() as session:
            total = await session.scalar(select(func.count(Payment.id)))
        return {"payments": int(total or 0)}


app = create_app()
