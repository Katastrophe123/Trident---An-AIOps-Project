"""Pydantic models for the agent: evidence in, diagnosis out, API shapes.

The :class:`Diagnosis` model is the important one. It is the contract the LLM
has to satisfy, and it is validated rather than trusted — a model that returns
prose, or invents a service name, or reports a confidence of 1.5, is treated as
a malformed response and re-asked.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The services the agent is allowed to talk about. Constraining this is what
#: stops the LLM attributing a fault to a service that does not exist.
KnownService = Literal["orders", "payments", "inventory", "postgres", "unknown"]

IncidentStatus = Literal["open", "diagnosed", "escalated", "resolved", "failed"]
DiagnosisSource = Literal["llm", "stub"]
ToolRisk = Literal["safe", "risky"]


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
class LogEvidence(BaseModel):
    """What ``get_logs`` returns for one service."""

    service: str
    minutes: float
    #: Newest first. Trimmed before it reaches the prompt.
    records: list[dict[str, Any]] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    #: Set when the service could not be reached at all — itself a strong signal.
    unreachable: bool = False
    error: str | None = None


class MetricEvidence(BaseModel):
    """What ``get_metrics`` returns for one service."""

    service: str
    requests: int | None = None
    errors: int | None = None
    error_rate: float | None = None
    average_latency_ms: float | None = None
    recent_average_latency_ms: float | None = None
    recent_p95_latency_ms: float | None = None
    memory_mb: float | None = None
    uptime_seconds: float | None = None
    downstream_errors: dict[str, int] = Field(default_factory=dict)
    unreachable: bool = False
    error: str | None = None


class ContainerEvidence(BaseModel):
    """One container's state, as reported by the Docker API."""

    service: str
    name: str
    #: running | exited | restarting | created | paused | dead | unknown
    state: str
    status: str = ""
    health: str | None = None
    restart_count: int = 0
    exit_code: int | None = None
    started_at: str | None = None


class ContainerStatusEvidence(BaseModel):
    """What ``get_container_status`` returns for the whole compose project."""

    containers: list[ContainerEvidence] = Field(default_factory=list)
    #: Services whose container has stopped and stayed stopped - a crash.
    not_running: list[str] = Field(default_factory=list)
    #: Services whose container is mid-transition (created, restarting,
    #: paused). Kept apart from ``not_running`` because they mean different
    #: things: one is an outage, the other is a deploy in progress, and
    #: conflating them makes the agent diagnose a transient state as a crash.
    transitioning: list[str] = Field(default_factory=list)
    available: bool = True
    error: str | None = None


class Evidence(BaseModel):
    """Everything the gather node collected, in one object."""

    logs: list[LogEvidence] = Field(default_factory=list)
    metrics: list[MetricEvidence] = Field(default_factory=list)
    containers: ContainerStatusEvidence = Field(default_factory=ContainerStatusEvidence)
    #: Tool name -> error, for tools that failed. Kept rather than raised: a
    #: diagnosis made from partial evidence is better than no diagnosis, but
    #: the gaps have to be visible in the trace.
    failures: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Diagnosis — the LLM's structured output
# --------------------------------------------------------------------------- #
class ProposedAction(BaseModel):
    """A remediation the model suggests. Never executed in Phase 2."""

    tool: str
    target: str | None = None
    reason: str = ""


class Diagnosis(BaseModel):
    """The structured answer the diagnose node demands from the model."""

    model_config = ConfigDict(extra="ignore")

    root_cause: Annotated[str, Field(min_length=3, max_length=1000)]
    #: Which service is actually at fault, which is not always the one that
    #: alerted. Database exhaustion is the case that separates the two.
    affected_service: KnownService
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    proposed_fix: Annotated[str, Field(min_length=3, max_length=1000)]
    #: Why the model concluded this, so a human can check the reasoning rather
    #: than only the answer.
    reasoning: str = ""
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    #: Which chaos scenario this matches, when the model recognises one. Used
    #: by the Phase 4 evaluation script to score against the known root cause.
    scenario: str | None = None

    @field_validator("root_cause", "proposed_fix", "reasoning")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class PostmortemReport(BaseModel):
    """The written report the postmortem node produces."""

    model_config = ConfigDict(extra="ignore")

    summary: Annotated[str, Field(min_length=3, max_length=500)]
    body: Annotated[str, Field(min_length=10, max_length=16000)]


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
class IncidentCreateRequest(BaseModel):
    """``POST /incidents`` — trigger an investigation by hand."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"service": "payments", "trigger": "manual investigation"}}
    )

    service: KnownService = "unknown"
    trigger: Annotated[str, Field(min_length=1, max_length=255)] = "manual trigger"
    #: Wait for the graph to finish and return the full result. False returns
    #: immediately with the incident id and runs the graph in the background.
    wait: bool = True


class StepView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    node: str
    status: str
    latency_ms: float
    error: str | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None


class ActionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tool: str
    target: str | None = None
    risk: ToolRisk
    status: str
    approved_by: str | None = None
    result: str | None = None


class IncidentSummaryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    service: str
    status: IncidentStatus
    trigger: str
    source: str
    root_cause: str | None = None
    root_cause_service: str | None = None
    confidence: float | None = None
    diagnosis_source: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class IncidentDetailView(IncidentSummaryView):
    """Full trace: every node, its evidence and its decision."""

    proposed_fix: str | None = None
    error: str | None = None
    attempts: int = 0
    steps: list[StepView] = Field(default_factory=list)
    actions: list[ActionView] = Field(default_factory=list)
    postmortem: PostmortemView | None = None


class PostmortemView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    summary: str
    body: str
    source: DiagnosisSource
    created_at: datetime


class ToolView(BaseModel):
    """One entry from the tool allowlist, as served by ``GET /tools``."""

    name: str
    risk: ToolRisk
    description: str
    #: False for the action tools, which exist but are not reachable from the
    #: graph until Phase 3 adds the approval gate.
    enabled_in_graph: bool


IncidentDetailView.model_rebuild()
