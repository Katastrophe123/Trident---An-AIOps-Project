"""SQLAlchemy models for the demo store.

Three tables, one per service, sharing one database — which is exactly why
"database exhaustion" takes down all three at once, and why the agent has to
distinguish "orders is broken" from "orders' dependency is broken".

Phase 2 adds the agent's own tables (``incidents``, ``incident_steps``,
``actions``, ``postmortems``) alongside these.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TimestampType = DateTime(timezone=True)


def utcnow() -> datetime:
    """Python-side default so SQLite and PostgreSQL agree on the shape."""
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    """Base class for all demo-store models."""


class Order(Base):
    """An order created by orders-service after calling the other two."""

    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    #: pending | confirmed | rejected | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    #: Why a non-confirmed order ended up that way - the first thing a human reads.
    failure_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TimestampType, nullable=False, default=utcnow, index=True
    )

    __table_args__ = (Index("ix_orders_status_created_at", "status", "created_at"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Order id={self.id} status={self.status}>"


class Payment(Base):
    """A fake payment recorded by payments-service."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    #: captured | declined
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        TimestampType, nullable=False, default=utcnow, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Payment id={self.id} status={self.status}>"


class InventoryItem(Base):
    """Stock level for a SKU, owned by inventory-service."""

    __tablename__ = "inventory_items"

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    available: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        TimestampType, nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<InventoryItem sku={self.sku} available={self.available}>"


class Reservation(Base):
    """A stock reservation held against an order."""

    __tablename__ = "reservations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(
        String(64), ForeignKey("inventory_items.sku", ondelete="CASCADE"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TimestampType, nullable=False, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Reservation order_id={self.order_id} sku={self.sku}>"


#: Seeded by the inventory service on startup so traffic always has stock.
SEED_SKUS: tuple[tuple[str, int], ...] = (
    ("SKU-LAPTOP-13", 500),
    ("SKU-LAPTOP-15", 500),
    ("SKU-DOCK-USBC", 500),
    ("SKU-MOUSE-WL", 500),
    ("SKU-KEYB-MECH", 500),
)


# --------------------------------------------------------------------------- #
# Phase 2: the agent's own tables
#
# Deliberately in the same database and the same metadata as the demo store.
# One migration history, one connection story - and it means the agent's record
# of an incident sits next to the orders that were failing during it.
# --------------------------------------------------------------------------- #
class Incident(Base):
    """One investigation, from alert to postmortem."""

    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    #: The service the alert fired for. Not necessarily the service at fault -
    #: database exhaustion alerts on the innocent services, which is the whole
    #: reason this and `root_cause_service` are separate columns.
    service: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: open | diagnosed | escalated | resolved | failed
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: What tripped the alert, in words: "health check returned unhealthy".
    trigger: Mapped[str] = mapped_column(String(255), nullable=False)
    #: manual | monitor - how the investigation started.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")

    root_cause: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    root_cause_service: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    proposed_fix: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    #: llm | stub - recorded so a diagnosis produced without a real model is
    #: never mistaken for one that used it.
    diagnosis_source: Mapped[str | None] = mapped_column(String(16), nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(TimestampType, nullable=True)

    __table_args__ = (Index("ix_incidents_status_created", "status", "created_at"),)


class IncidentStep(Base):
    """One graph node execution: what went in, what came out, how long it took.

    This is the audit trail. An agent that cannot show its working is not
    trustworthy, so every node writes a row here whether it succeeds or not.
    """

    __tablename__ = "incident_steps"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Execution order within the incident, so the trace reads top to bottom.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ok | error
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    #: JSON-encoded rather than JSONB: the same schema has to run on SQLite in
    #: the test suite, and nothing queries inside these blobs.
    input: Mapped[str | None] = mapped_column(String(8000), nullable=True)
    output: Mapped[str | None] = mapped_column(String(8000), nullable=True)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    latency_ms: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)

    __table_args__ = (Index("ix_incident_steps_incident_sequence", "incident_id", "sequence"),)


class Action(Base):
    """A remediation the agent proposed, and what became of it.

    Written in Phase 2 with ``status='proposed'`` and never executed - the
    agent has no action tools wired into the graph yet. Phase 3 adds the
    approval gate that moves a row to approved/rejected/applied.
    """

    __tablename__ = "actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Must be a name from the tool allowlist; free text is never executed.
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: safe | risky - decides whether Phase 3 needs human approval.
    risk: Mapped[str] = mapped_column(String(16), nullable=False, default="risky")
    #: proposed | approved | rejected | applied | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)


class Postmortem(Base):
    """The written report. One per incident."""

    __tablename__ = "postmortems"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Markdown: timeline, root cause, actions taken, prevention.
    body: Mapped[str] = mapped_column(String(16000), nullable=False)
    #: llm | stub, same reasoning as Incident.diagnosis_source.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="llm")
    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)
