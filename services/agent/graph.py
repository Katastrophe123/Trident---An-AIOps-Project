"""The LangGraph workflow. Phase 2: linear, diagnosis-only.

    gather_evidence  ->  diagnose  ->  postmortem

The plan's build order is explicit that this phase stops here: "Build the
tools, then a linear graph (gather -> diagnose -> postmortem) with no fixing
yet. Get the diagnosis right before the agent is allowed to act." So there is
no approval gate, no remediation and no retry loop, and the two risky tools are
not reachable from any node here.

What Phase 3 adds, and why it is not here:

* **Parallel evidence gathering as three nodes.** One node currently calls the
  three tools concurrently with ``asyncio.gather``, which gets the latency win
  without the fan-out. Three separate graph nodes matter when each can fail,
  retry and be checkpointed independently - that is a control-flow change, and
  control flow is Phase 3.
* **The low-confidence branch.** ``needs_more_evidence`` is computed and
  recorded, so the branch condition is already measurable before the edge
  exists.
* **Approval gate, apply-fix, verify, retry cap, escalation, checkpointing.**

Every node wraps itself in :func:`_run_node`, so a node that raises becomes a
recorded ``error`` step rather than an exception that loses the whole trace. An
agent whose failure mode is "no output at all" cannot be debugged.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from services.agent.config import AgentSettings
from services.agent.llm import LLMClient, LLMError, LLMFormatError
from services.agent.prompts import (
    DIAGNOSE_SYSTEM,
    POSTMORTEM_SYSTEM,
    diagnose_user_prompt,
    postmortem_user_prompt,
    render_evidence,
)
from services.agent.schemas import (
    ContainerStatusEvidence,
    Diagnosis,
    Evidence,
    LogEvidence,
    MetricEvidence,
    PostmortemReport,
    ProposedAction,
)
from services.agent.state import AgentState, StepRecord
from services.agent.tools import Toolbox, is_allowed
from services.common.logging_setup import get_logger

logger = get_logger(__name__)

#: Below this, the diagnosis is not actionable. Phase 3 branches back to
#: gathering more evidence; Phase 2 records it and escalates.
CONFIDENCE_FLOOR = 0.4


@dataclass(slots=True)
class NodeOutcome:
    """What a node produced, plus what to write into the trace."""

    updates: dict[str, Any]
    output: dict[str, Any] | None = None
    error: str | None = None


NodeFn = Callable[[AgentState], Awaitable[NodeOutcome]]


async def _run_node(name: str, state: AgentState, fn: NodeFn) -> dict[str, Any]:
    """Time a node, record a step for it, and never let it raise.

    Returns the state delta. The step is appended to the delta rather than
    mutated onto the incoming state, because LangGraph merges returned values
    and mutating the input would be lost.
    """
    started = time.perf_counter()
    try:
        outcome = await fn(state)
        status, error = ("error", outcome.error) if outcome.error else ("ok", None)
        updates, output = outcome.updates, outcome.output
    except Exception as exc:  # noqa: BLE001 - a node failure is data, not a crash
        logger.exception("agent_node_failed", extra={"node": name})
        status = "error"
        error = f"{type(exc).__name__}: {exc}"[:600]
        updates, output = {"error": error}, None

    latency_ms = (time.perf_counter() - started) * 1000
    step = StepRecord(
        sequence=state.next_sequence(),
        node=name,
        status=status,
        latency_ms=round(latency_ms, 2),
        error=error,
        output=output,
    )
    logger.info(
        "agent_node_completed",
        extra={
            "node": name,
            "incident_id": state.incident_id,
            "status": status,
            "latency_ms": round(latency_ms, 2),
        },
    )
    return {**updates, "steps": [*state.steps, step]}


# --------------------------------------------------------------------------- #
# Node 1 (plan node 2): gather evidence
# --------------------------------------------------------------------------- #
def make_gather_node(toolbox: Toolbox, settings: AgentSettings) -> NodeFn:
    """Pull logs, metrics and container status for every service."""

    async def gather(state: AgentState) -> NodeOutcome:
        services = toolbox.services

        # Concurrent, not sequential: three services x two HTTP calls plus a
        # Docker round trip is a second of wall-clock if serialised, and during
        # an incident the evidence should be a snapshot, not a slideshow.
        log_calls = [toolbox.get_logs(service) for service in services]
        metric_calls = [toolbox.get_metrics(service) for service in services]
        results = await asyncio.gather(
            *log_calls,
            *metric_calls,
            toolbox.get_container_status(),
            return_exceptions=True,
        )

        count = len(services)
        failures: dict[str, str] = {}

        logs: list[LogEvidence] = []
        for service, result in zip(services, results[:count], strict=True):
            if isinstance(result, BaseException):
                failures[f"get_logs({service})"] = f"{type(result).__name__}: {result}"[:300]
            else:
                logs.append(cast(LogEvidence, result))

        metrics: list[MetricEvidence] = []
        for service, result in zip(services, results[count : 2 * count], strict=True):
            if isinstance(result, BaseException):
                failures[f"get_metrics({service})"] = f"{type(result).__name__}: {result}"[:300]
            else:
                metrics.append(cast(MetricEvidence, result))

        container_result = results[2 * count]
        containers: ContainerStatusEvidence
        if isinstance(container_result, BaseException):
            failures["get_container_status"] = (
                f"{type(container_result).__name__}: {container_result}"[:300]
            )
            containers = ContainerStatusEvidence(
                available=False, error=failures["get_container_status"]
            )
        else:
            containers = cast(ContainerStatusEvidence, container_result)
            if not containers.available and containers.error:
                failures["get_container_status"] = containers.error

        evidence = Evidence(logs=logs, metrics=metrics, containers=containers, failures=failures)

        unreachable = sorted(
            {entry.service for entry in metrics if entry.unreachable}
            | {entry.service for entry in logs if entry.unreachable}
        )
        return NodeOutcome(
            updates={"evidence": evidence},
            output={
                "services_polled": services,
                "log_records": sum(len(entry.records) for entry in logs),
                "unreachable_services": unreachable,
                "containers_not_running": containers.not_running,
                "tool_failures": failures,
                "log_window_minutes": settings.evidence_log_minutes,
            },
        )

    return gather


# --------------------------------------------------------------------------- #
# Node 2 (plan node 3): diagnose
# --------------------------------------------------------------------------- #
def make_diagnose_node(llm: LLMClient, settings: AgentSettings) -> NodeFn:
    """Ask the model for a structured diagnosis, validated by Pydantic."""

    async def diagnose(state: AgentState) -> NodeOutcome:
        user = diagnose_user_prompt(
            alert_service=state.alert_service,
            trigger=state.trigger,
            evidence=state.evidence,
            alert_detail=state.alert_detail,
        )
        context = {
            "task": "diagnose",
            "alert_service": state.alert_service,
            "evidence": render_evidence(state.evidence),
        }

        try:
            diagnosis, model_name = await llm.complete_model(
                system=DIAGNOSE_SYSTEM, user=user, schema=Diagnosis, context=context
            )
        except (LLMError, LLMFormatError) as exc:
            # Escalate rather than guess. A diagnosis is the one thing this
            # phase exists to produce, so failing to produce one is a reportable
            # outcome, not a silent default.
            return NodeOutcome(
                updates={"status": "escalated", "error": f"{type(exc).__name__}: {exc}"[:600]},
                error=f"{type(exc).__name__}: {exc}"[:600],
            )

        # Drop any proposed action naming a tool that is not allowlisted. This
        # is the enforcement point for "the LLM never runs arbitrary commands":
        # the model's output is data, and data that does not match the registry
        # is discarded here rather than dispatched later.
        kept: list[ProposedAction] = []
        rejected: list[ProposedAction] = []
        for action in diagnosis.proposed_actions:
            (kept if is_allowed(action.tool) else rejected).append(action)
        if rejected:
            logger.warning(
                "agent_rejected_unknown_tools",
                extra={
                    "incident_id": state.incident_id,
                    "rejected": [action.tool for action in rejected],
                },
            )
            diagnosis = diagnosis.model_copy(update={"proposed_actions": kept})

        low_confidence = diagnosis.confidence < CONFIDENCE_FLOOR
        return NodeOutcome(
            updates={
                "diagnosis": diagnosis,
                "diagnosis_source": llm.source,
                "diagnosis_model": model_name,
                "status": "escalated" if low_confidence else "diagnosed",
            },
            output={
                "root_cause": diagnosis.root_cause,
                "affected_service": diagnosis.affected_service,
                "confidence": diagnosis.confidence,
                "scenario": diagnosis.scenario,
                "proposed_fix": diagnosis.proposed_fix,
                "proposed_actions": [action.model_dump() for action in kept],
                "rejected_actions": [action.tool for action in rejected],
                "source": llm.source,
                "model": model_name,
                # Recorded, not acted on: this is the Phase 3 branch condition,
                # measurable before the edge exists.
                "needs_more_evidence": low_confidence,
                "confidence_floor": CONFIDENCE_FLOOR,
            },
        )

    return diagnose


# --------------------------------------------------------------------------- #
# Node 3 (plan node 6): postmortem
# --------------------------------------------------------------------------- #
def make_postmortem_node(llm: LLMClient) -> NodeFn:
    """Write the report. Runs even when the diagnosis failed."""

    async def postmortem(state: AgentState) -> NodeOutcome:
        if state.diagnosis is None:
            # No diagnosis to write up. Produce the report locally rather than
            # spending a request asking a model to describe a failure it was
            # not part of.
            report = PostmortemReport(
                summary=f"{state.alert_service}: investigation failed without a diagnosis",
                body=(
                    "## Summary\n\nThe investigation could not produce a diagnosis.\n\n"
                    "## Timeline\n\n"
                    + "\n".join(f"- {line}" for line in state.timeline())
                    + f"\n\n## Root cause\n\nNot determined.\n\n### Error\n\n"
                    f"```\n{state.error or 'unknown'}\n```\n\n"
                    "## Actions taken\n\nNone.\n\n"
                    "## Prevention\n\n- Make the diagnosis step's failure visible in "
                    "the alert path, so a broken agent is not mistaken for a healthy "
                    "system.\n"
                ),
            )
            return NodeOutcome(
                updates={"postmortem": report, "status": state.status or "escalated"},
                output={"summary": report.summary, "source": "local", "diagnosed": False},
            )

        user = postmortem_user_prompt(
            diagnosis=state.diagnosis,
            timeline=state.timeline(),
            alert_service=state.alert_service,
        )
        context = {
            "task": "postmortem",
            "diagnosis": state.diagnosis.model_dump(mode="json"),
            "timeline": state.timeline(),
            "alert_service": state.alert_service,
        }

        try:
            report, _ = await llm.complete_model(
                system=POSTMORTEM_SYSTEM, user=user, schema=PostmortemReport, context=context
            )
        except (LLMError, LLMFormatError) as exc:
            # A missing report must not discard a good diagnosis, so fall back
            # to a locally-rendered one and keep the diagnosed status.
            report = PostmortemReport(
                summary=f"{state.diagnosis.affected_service}: {state.diagnosis.root_cause[:180]}",
                body=(
                    f"## Summary\n\n{state.diagnosis.root_cause}\n\n"
                    "## Timeline\n\n"
                    + "\n".join(f"- {line}" for line in state.timeline())
                    + f"\n\n## Root cause\n\n{state.diagnosis.root_cause}\n\n"
                    f"Affected service: **{state.diagnosis.affected_service}**. "
                    f"Confidence: {state.diagnosis.confidence}.\n\n"
                    "## Actions taken\n\nNone - the agent is in diagnosis-only mode. "
                    f"Recommended fix:\n\n> {state.diagnosis.proposed_fix}\n\n"
                    "## Prevention\n\n- Alert on the cause, not only the symptom.\n\n"
                    f"_Report generated locally; the model was unavailable: {exc}_\n"
                ),
            )
            return NodeOutcome(
                updates={"postmortem": report},
                output={"summary": report.summary, "source": "local_fallback"},
                error=f"postmortem fell back to a local report: {exc}"[:600],
            )

        return NodeOutcome(
            updates={"postmortem": report},
            output={"summary": report.summary, "source": llm.source},
        )

    return postmortem


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def build_graph(*, toolbox: Toolbox, llm: LLMClient, settings: AgentSettings) -> Any:
    """Compile the Phase 2 linear graph."""
    gather = make_gather_node(toolbox, settings)
    diagnose = make_diagnose_node(llm, settings)
    postmortem = make_postmortem_node(llm)

    async def gather_node(state: AgentState) -> dict[str, Any]:
        return await _run_node("gather_evidence", state, gather)

    async def diagnose_node(state: AgentState) -> dict[str, Any]:
        return await _run_node("diagnose", state, diagnose)

    async def postmortem_node(state: AgentState) -> dict[str, Any]:
        return await _run_node("postmortem", state, postmortem)

    graph = StateGraph(AgentState)
    graph.add_node("gather_evidence", gather_node)
    graph.add_node("diagnose", diagnose_node)
    graph.add_node("postmortem", postmortem_node)

    graph.add_edge(START, "gather_evidence")
    graph.add_edge("gather_evidence", "diagnose")
    graph.add_edge("diagnose", "postmortem")
    graph.add_edge("postmortem", END)

    # No checkpointer in Phase 2: nothing in a linear, non-interruptible graph
    # can be resumed, so a PostgreSQL checkpointer would add a dependency and
    # a failure mode in exchange for nothing. Phase 3's approval interrupt is
    # what makes it necessary.
    return graph.compile()


async def run_investigation(
    compiled: Any,
    *,
    incident_id: str,
    alert_service: str,
    trigger: str,
    alert_detail: dict[str, Any] | None = None,
) -> AgentState:
    """Run the graph once and return the final state."""
    initial = AgentState(
        incident_id=incident_id,
        alert_service=alert_service,
        trigger=trigger,
        alert_detail=alert_detail,
    )
    raw = await compiled.ainvoke(initial)
    # LangGraph returns a plain dict even for a Pydantic state schema.
    return AgentState.model_validate(raw)
