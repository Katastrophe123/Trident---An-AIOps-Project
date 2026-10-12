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
