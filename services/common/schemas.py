"""Pydantic v2 models shared across services.

The health and metrics shapes are a contract: Phase 2's ``get_metrics`` and the
alert monitor both parse them, so they live here rather than in each service.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ServiceStatus = Literal["healthy", "degraded", "unhealthy"]


class DependencyHealth(BaseModel):
    """Health of one thing a service depends on."""

    name: str
    status: ServiceStatus
    latency_ms: float | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    """``GET /health``.

    ``status`` is the field the alert monitor polls:

    * ``healthy``   - everything works
    * ``degraded``  - serving, but a dependency is slow or partially failing
    * ``unhealthy`` - cannot serve correctly (returns HTTP 503)
    """

    status: ServiceStatus
    service: str
    version: str
    uptime_seconds: float
    dependencies: list[DependencyHealth] = Field(default_factory=list)
    detail: str | None = None


class EndpointMetrics(BaseModel):
    requests: int
    errors: int
    average_latency_ms: float


class MetricsResponse(BaseModel):
    """``GET /metrics`` — the four readings the plan asks for, plus context."""

    service: str
    uptime_seconds: float
    requests: int
    errors: int
    error_rate: float
    average_latency_ms: float
    recent_average_latency_ms: float
    recent_p95_latency_ms: float
    memory_mb: float
    status_counts: dict[str, int] = Field(default_factory=dict)
    downstream_errors: dict[str, int] = Field(default_factory=dict)
    endpoints: dict[str, EndpointMetrics] = Field(default_factory=dict)


class LogRecord(BaseModel):
    """One line from a service's in-memory log buffer."""

    model_config = ConfigDict(extra="allow")

    timestamp: str
    level: str
    service: str
    event: str
    request_id: str | None = None


class LogsResponse(BaseModel):
    """``GET /_logs`` — newest first."""

    service: str
    count: int
    records: list[LogRecord]


class ChaosStateResponse(BaseModel):
    """``GET /_chaos`` — what faults are currently injected in this process."""

    service: str
    latency_ms: int
    memory_leak_active: bool
    leaked_mb: int
    held_db_connections: int
    healthy: bool


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """One envelope for every error, with the correlating request id."""

    error: ErrorDetail
    request_id: str | None = None


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
class OrderRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {"customer_id": "cust-0042", "sku": "SKU-LAPTOP-13", "quantity": 1}
        }
    )

    customer_id: Annotated[str, Field(min_length=1, max_length=64)]
    sku: Annotated[str, Field(min_length=1, max_length=64)]
    quantity: Annotated[int, Field(ge=1, le=50)] = 1


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: str
    sku: str
    quantity: int
    amount: float
    status: str
    failure_reason: str | None = None
    created_at: datetime


# --------------------------------------------------------------------------- #
# Payments
# --------------------------------------------------------------------------- #
class PaymentRequest(BaseModel):
    # A real UUID type, not Any: otherwise a malformed id passes validation and
    # fails later as a 500, when it should be a 422 at the boundary.
    order_id: uuid.UUID
    amount: Annotated[float, Field(gt=0)]


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    amount: float
    status: str
    created_at: datetime


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
class ReservationRequest(BaseModel):
    order_id: uuid.UUID
    sku: Annotated[str, Field(min_length=1, max_length=64)]
    quantity: Annotated[int, Field(ge=1, le=50)]


class ReservationResponse(BaseModel):
    reserved: bool
    sku: str
    quantity: int
    available_after: int
    reason: str | None = None


class StockResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sku: str
    available: int
    reserved: int
