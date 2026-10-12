"""The graph state: one typed object passed between all nodes.

The plan calls for exactly this — "one typed object passed between all nodes.
It holds the alert, collected evidence, the current hypothesis with a
confidence score, proposed actions, approval status, attempt count and final
outcome."

Phase 2's linear graph only fills part of it. The fields Phase 3 needs
(``approval_status``, ``attempts``, ``verification``) are declared now and
documented as unused, because adding a field to a state object that is already
being checkpointed is more disruptive than leaving room for it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from services.agent.schemas import Diagnosis, Evidence, PostmortemReport

ApprovalStatus = Literal["not_required", "pending", "approved", "rejected"]


class StepRecord(BaseModel):
    """One node execution, accumulated in state and persisted afterwards."""

    sequence: int
    node: str
    status: str = "ok"
    latency_ms: float = 0.0
    error: str | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None


class AgentState(BaseModel):
    """Everything an investigation knows about itself."""

    # --- the alert --------------------------------------------------------
    incident_id: str
    #: The service that alerted. Not necessarily the one at fault.
    alert_service: str = "unknown"
    trigger: str = ""
    #: Raw /health body at the moment the incident opened, when there was one.
    alert_detail: dict[str, Any] | None = None

    # --- evidence ---------------------------------------------------------
    evidence: Evidence = Field(default_factory=Evidence)

    # --- hypothesis -------------------------------------------------------
    diagnosis: Diagnosis | None = None
    #: llm | stub, carried so the API can label the answer honestly.
    diagnosis_source: str | None = None
    diagnosis_model: str | None = None

    # --- outcome ----------------------------------------------------------
    postmortem: PostmortemReport | None = None
    status: str = "open"
    error: str | None = None

    # --- audit trail ------------------------------------------------------
    steps: list[StepRecord] = Field(default_factory=list)

    # --- Phase 3 placeholders --------------------------------------------
    #: Phase 3: set to "pending" when a risky action needs a human, which is
    #: what the LangGraph interrupt suspends on.
    approval_status: ApprovalStatus = "not_required"
    #: Phase 3: fix-and-verify attempts, capped at 3 before escalating.
    attempts: int = 0
    #: Phase 3: health re-check result after a fix is applied.
    verification: dict[str, Any] | None = None

    def next_sequence(self) -> int:
        return len(self.steps) + 1

    @property
    def confidence(self) -> float:
        return self.diagnosis.confidence if self.diagnosis else 0.0

    def timeline(self) -> list[str]:
        """Human-readable step list, for the postmortem prompt."""
        lines = [f"Alert: {self.trigger} (service: {self.alert_service})"]
        lines += [
            f"{step.node}: {step.status} in {step.latency_ms:.0f} ms"
            + (f" - {step.error}" if step.error else "")
            for step in self.steps
        ]
        return lines
