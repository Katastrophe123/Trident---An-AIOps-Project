"""Reading and writing incidents.

All SQL lives here so the graph nodes stay pure: a node takes state and returns
state, and nothing in it knows about a session. That is what makes the nodes
testable without a database, and it is why persistence happens once, after the
graph finishes, rather than inside each node.

The trade-off is explicit: if the process dies mid-investigation, that
investigation leaves no trace. Phase 3's checkpointer is what fixes that, and
until the graph can be interrupted there is nothing to resume anyway.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import desc, select

from services.agent.schemas import Diagnosis
from services.agent.state import AgentState
from services.agent.tools import risk_of
from services.common.db import Database
from services.common.logging_setup import get_logger
from services.common.models import Action, Incident, IncidentStep, Postmortem, utcnow

logger = get_logger(__name__)

#: Step input/output columns are bounded strings, so a huge evidence blob has to
#: be truncated rather than silently rejected by the database.
MAX_BLOB = 7800


def _dump(payload: dict[str, Any] | None) -> str | None:
    """JSON-encode a step payload, truncating rather than overflowing."""
    if payload is None:
        return None
    text = json.dumps(payload, default=str)
    if len(text) <= MAX_BLOB:
        return text
    return json.dumps({"truncated": True, "bytes": len(text), "head": text[:MAX_BLOB]}, default=str)


def _load(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"unparseable": text[:500]}
    return value if isinstance(value, dict) else {"value": value}


class IncidentRepository:
    """Persistence for incidents, steps, proposed actions and postmortems."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def open_incident(
        self,
        *,
        service: str,
        trigger: str,
        source: str = "manual",
    ) -> uuid.UUID:
        """Create an ``open`` incident and return its id."""
        incident_id = uuid.uuid4()
        async with self._database.session() as session:
            session.add(
                Incident(
                    id=incident_id,
                    service=service,
                    status="open",
                    trigger=trigger[:255],
                    source=source,
                )
            )
        logger.info(
            "incident_opened",
            extra={"incident_id": str(incident_id), "service": service, "source": source},
        )
        return incident_id

    async def save_result(self, state: AgentState) -> None:
        """Write the finished investigation: steps, diagnosis, actions, report."""
        incident_uuid = uuid.UUID(state.incident_id)

        async with self._database.session() as session:
            incident = await session.get(Incident, incident_uuid)
            if incident is None:  # pragma: no cover - only if a row was deleted
                logger.error("incident_missing_on_save", extra={"incident_id": state.incident_id})
                return

            diagnosis: Diagnosis | None = state.diagnosis
            incident.status = state.status or "open"
            incident.attempts = state.attempts
            incident.error = (state.error or None) and state.error[:1000]
            incident.diagnosis_source = state.diagnosis_source

            if diagnosis is not None:
                incident.root_cause = diagnosis.root_cause[:1000]
                incident.root_cause_service = diagnosis.affected_service
                incident.confidence = diagnosis.confidence
                incident.proposed_fix = diagnosis.proposed_fix[:1000]

            for step in state.steps:
                session.add(
                    IncidentStep(
                        incident_id=incident_uuid,
                        sequence=step.sequence,
                        node=step.node,
                        status=step.status,
                        input=_dump(step.input),
                        output=_dump(step.output),
                        error=step.error and step.error[:1000],
                        latency_ms=step.latency_ms,
                    )
                )

            # Proposed only. Phase 2 has no execution path, so a row here is a
            # recommendation and its status says so.
            if diagnosis is not None:
                for action in diagnosis.proposed_actions:
                    session.add(
                        Action(
                            incident_id=incident_uuid,
                            tool=action.tool[:64],
                            target=(action.target or None) and action.target[:64],
                            risk=risk_of(action.tool),
                            status="proposed",
                            result=None,
                        )
                    )

            if state.postmortem is not None:
                session.add(
                    Postmortem(
                        incident_id=incident_uuid,
                        summary=state.postmortem.summary[:500],
                        body=state.postmortem.body[:16000],
                        source=state.diagnosis_source or "stub",
                    )
                )

        logger.info(
            "incident_saved",
            extra={
                "incident_id": state.incident_id,
                "status": state.status,
                "steps": len(state.steps),
                "confidence": state.confidence,
            },
        )

    async def mark_failed(self, incident_id: uuid.UUID, error: str) -> None:
        """Record that the investigation itself blew up."""
        async with self._database.session() as session:
            incident = await session.get(Incident, incident_id)
            if incident is not None:
                incident.status = "failed"
                incident.error = error[:1000]

    async def list_incidents(
        self, *, limit: int = 50, status: str | None = None, service: str | None = None
    ) -> list[Incident]:
        async with self._database.session() as session:
            query = select(Incident).order_by(desc(Incident.created_at)).limit(limit)
            if status:
                query = query.where(Incident.status == status)
            if service:
                query = query.where(Incident.service == service)
            return list((await session.scalars(query)).all())

    async def get_incident(self, incident_id: uuid.UUID) -> Incident | None:
        async with self._database.session() as session:
            return await session.get(Incident, incident_id)

    async def get_detail(
        self, incident_id: uuid.UUID
    ) -> tuple[Incident, list[IncidentStep], list[Action], Postmortem | None] | None:
        """Everything needed for the full-trace view, in one session."""
        async with self._database.session() as session:
            incident = await session.get(Incident, incident_id)
            if incident is None:
                return None

            steps = list(
                (
                    await session.scalars(
                        select(IncidentStep)
                        .where(IncidentStep.incident_id == incident_id)
                        .order_by(IncidentStep.sequence)
                    )
                ).all()
            )
            actions = list(
                (
                    await session.scalars(
                        select(Action)
                        .where(Action.incident_id == incident_id)
                        .order_by(Action.created_at)
                    )
                ).all()
            )
            postmortem = (
                await session.scalars(
                    select(Postmortem).where(Postmortem.incident_id == incident_id)
                )
            ).first()
            return incident, steps, actions, postmortem

    async def get_postmortem(self, incident_id: uuid.UUID) -> Postmortem | None:
        async with self._database.session() as session:
            return (
                await session.scalars(
                    select(Postmortem).where(Postmortem.incident_id == incident_id)
                )
            ).first()

    #: Statuses that mean "this incident is still live".
    UNRESOLVED = ("open", "diagnosed", "escalated")

    async def unresolved_incident_for(self, service: str) -> Incident | None:
        """A still-live incident for this service, if there is one.

        The monitor uses this to avoid opening a new incident every poll while
        a service stays down - without it, a two-minute outage at a five-second
        poll interval produces twenty-four identical investigations.

        This only works because :meth:`resolve_incidents_for` closes incidents
        when the service recovers. Without that half, the first incident per
        service would silence the monitor for that service forever - which is
        exactly what happened the first time this ran.
        """
        async with self._database.session() as session:
            return (
                await session.scalars(
                    select(Incident)
                    .where(Incident.service == service)
                    .where(Incident.status.in_(self.UNRESOLVED))
                    .order_by(desc(Incident.created_at))
                    .limit(1)
                )
            ).first()

    async def resolve_incidents_for(self, service: str) -> int:
        """Close any live incident for a service that is healthy again.

        Recovery is the only resolution signal Phase 2 has: it applies no fix,
        so "the symptom stopped" is all it can observe. The incident keeps its
        diagnosis and postmortem; only the status and `resolved_at` change.
        """
        async with self._database.session() as session:
            incidents = list(
                (
                    await session.scalars(
                        select(Incident)
                        .where(Incident.service == service)
                        .where(Incident.status.in_(self.UNRESOLVED))
                    )
                ).all()
            )
            for incident in incidents:
                incident.status = "resolved"
                incident.resolved_at = utcnow()

        if incidents:
            logger.info(
                "incidents_resolved",
                extra={"service": service, "count": len(incidents)},
            )
        return len(incidents)


def step_payloads(steps: list[IncidentStep]) -> list[dict[str, Any]]:
    """Decode stored step blobs for the API layer."""
    return [
        {
            "sequence": step.sequence,
            "node": step.node,
            "status": step.status,
            "latency_ms": float(step.latency_ms),
            "error": step.error,
            "input": _load(step.input),
            "output": _load(step.output),
        }
        for step in steps
    ]


__all__ = ["IncidentRepository", "step_payloads"]
