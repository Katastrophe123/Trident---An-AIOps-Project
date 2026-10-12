"""A deterministic, rule-based stand-in for the LLM.

Not a test mock. This runs whenever no API key is configured, so the graph can
be demonstrated end to end offline, and everything it produces is labelled
``source="stub"`` through to the API and the stored postmortem.

It also serves as the **baseline** for the Phase 4 evaluation. Reporting "the
agent diagnosed 4/5 scenarios correctly" means little on its own; reporting it
against what a page of if-statements achieves on the same evidence is a real
measurement. If the model cannot beat this, that is the interesting result.

The rules are ordered by how *specific* the signal is, not by how common the
scenario is. An exited container is unambiguous; high memory is not.
"""

from __future__ import annotations

import json
import statistics
from typing import Any

from services.agent.llm import Completion, LLMProviderBase

#: A service slower than this is a candidate for "something is wrong", rather
#: than ordinary variation. Absolute, because the baseline here is single-digit
#: milliseconds and a ratio alone would fire on a 2ms -> 6ms wobble.
LATENCY_OUTLIER_FLOOR_MS = 500.0
#: The fastest service must be this many times faster than the slow ones for the
#: problem to be *localised* rather than system-wide.
LATENCY_SPREAD_RATIO = 3.0
#: The only caller in this system. orders is slow whenever a service it depends
#: on is slow, so it is the last candidate for blame, not the first.
CALLER_SERVICE = "orders"
#: Same idea for resident memory.
MEMORY_OUTLIER_RATIO = 1.5
MEMORY_OUTLIER_FLOOR_MB = 150.0

#: Substrings that identify a scenario from a log message. Checked in order.
AUTH_MARKERS = ("password authentication failed", "no pg_hba.conf entry")
EXHAUSTION_MARKERS = (
    "too many clients",
    "remaining connection slots",
    "too many connections",
    "queuepool limit",
    "pool timeout",
)
UNREACHABLE_MARKERS = (
    "name or service not known",
    "connection refused",
    "connect error",
    "unavailable",
)


def _log_text(evidence: dict[str, Any]) -> dict[str, str]:
    """All log text per service, lowercased, for substring matching."""
    joined: dict[str, str] = {}
    for entry in evidence.get("logs") or []:
        service = str(entry.get("service", "unknown"))
        parts: list[str] = []
        for record in entry.get("records") or []:
            for key in ("event", "error", "reason", "detail", "failure_reason"):
                value = record.get(key)
                if value:
                    parts.append(str(value))
        joined[service] = " || ".join(parts).lower()
    return joined


def _metric(evidence: dict[str, Any], field: str) -> dict[str, float]:
    """One numeric metric per reachable service."""
    out: dict[str, float] = {}
    for entry in evidence.get("metrics") or []:
        if entry.get("unreachable"):
            continue
        value = entry.get(field)
        if isinstance(value, (int, float)):
            out[str(entry.get("service", "unknown"))] = float(value)
    return out


def _slow_service(latency: dict[str, float]) -> tuple[str, float] | None:
    """Find the service responsible for a latency problem, if there is one.

    Written this way because the obvious version was wrong, and a realistic
    test caught it. Comparing the slowest service against the median of the
    others fails on exactly the case that matters: in a real slow-dependency
    incident **two** services are slow - the slow one and the caller waiting on
    it - so the median is dragged up and the signal cancels itself out. Real
    readings from a 3-second injection into inventory:

        orders 3086 ms | inventory 3021 ms | payments 5.6 ms

    The median of "the others" from orders' point of view is 1513 ms, a ratio
    of only 2.0, and a median-based rule sees nothing.

    So instead: find everything above an absolute floor, confirm the problem is
    localised by checking some service is still fast, then prefer a *callee*
    over the caller. ``orders`` is slow whenever its dependencies are slow, so
    blaming it would name the victim - the same mistake the db-exhaustion
    scenario punishes.
    """
    if len(latency) < 2:
        return None

    slow = {name: value for name, value in latency.items() if value >= LATENCY_OUTLIER_FLOOR_MS}
    if not slow:
        return None

    fastest = min(latency.values())
    # Everything slow means the problem is not attributable to one service -
    # that is a shared-resource or host-level issue, not this scenario.
    if fastest > 0 and (max(slow.values()) / fastest) < LATENCY_SPREAD_RATIO:
        return None

    downstream = {name: value for name, value in slow.items() if name != CALLER_SERVICE}
    candidates = downstream or slow
    worst = max(candidates, key=lambda key: candidates[key])
    return worst, candidates[worst]


def _outlier(values: dict[str, float], ratio: float, floor: float) -> tuple[str, float] | None:
    """The single service whose value stands out, if there is one.

    Relative to the median rather than an absolute threshold: "one service is
    far slower than its peers" is the signal, and a fixed millisecond cutoff
    would misfire on a loaded laptop.
    """
    if len(values) < 2:
        return None
    worst = max(values, key=lambda key: values[key])
    worst_value = values[worst]
    others = [v for k, v in values.items() if k != worst]
    baseline = statistics.median(others) if others else 0.0
    if worst_value < floor:
        return None
    if baseline > 0 and worst_value / baseline < ratio:
        return None
    return worst, worst_value


class StubProvider(LLMProviderBase):
    """Produces Diagnosis / PostmortemReport JSON without a model."""

    @property
    def source(self) -> str:
        return "stub"

    async def complete(
        self, *, system: str, user: str, context: dict[str, Any] | None = None
    ) -> Completion:
        context = context or {}
        task = str(context.get("task", "diagnose"))
        evidence = dict(context.get("evidence") or {})

        payload: dict[str, Any]
        if task == "postmortem":
            payload = self._postmortem(context)
        else:
            payload = self._diagnose(evidence, str(context.get("alert_service", "unknown")))

        return Completion(text=json.dumps(payload), model="stub-heuristic", source="stub")

    # --- diagnosis ---------------------------------------------------------
    def _diagnose(self, evidence: dict[str, Any], alert_service: str) -> dict[str, Any]:
        containers = dict(evidence.get("containers") or {})
        logs = _log_text(evidence)

        # 0. A container mid-transition is not evidence of anything yet. Said
        #    plainly at low confidence, because a `--force-recreate` passes
        #    through this state and diagnosing it as a crash hides the real
        #    fault underneath - observed happening during a bad-config
        #    injection, which the agent called `crashed_service` at 0.95.
        transitioning = [str(s) for s in (containers.get("transitioning") or [])]
        if transitioning and not (containers.get("not_running") or []):
            service = transitioning[0]
            return self._answer(
                root_cause=f"The {service} container is being started or replaced "
                f"right now, so the system is in transition rather than broken.",
                affected_service=service,
                confidence=0.3,
                proposed_fix=f"Wait for {service} to finish starting, then re-check. "
                f"If it is still failing once settled, investigate again.",
                reasoning="Docker reports the container in a transitional state "
                "(created/restarting), not exited. A deploy in progress looks like "
                "an outage to every caller but needs no action.",
                scenario=None,
                actions=[],
            )

        # 1. A container that has stopped and stayed stopped. Unambiguous, so
        #    highest priority and highest confidence.
        not_running = [str(s) for s in (containers.get("not_running") or [])]
        if not_running:
            service = not_running[0]
            return self._answer(
                root_cause=f"The {service} container is not running, so callers get "
                f"connection errors.",
                affected_service=service,
                confidence=0.95,
                proposed_fix=f"Restart the {service} container.",
                reasoning="Docker reports the container as not running, and callers "
                "report connection errors rather than HTTP error responses.",
                scenario="crashed_service",
                actions=[("restart_container", service, "the container is down")],
            )

        # 2. Database credentials. Also unambiguous - an auth error never
        #    resolves itself, so it is config, not load.
        for service, text in logs.items():
            if any(marker in text for marker in AUTH_MARKERS):
                return self._answer(
                    root_cause=f"{service} cannot authenticate to PostgreSQL: its "
                    f"DATABASE_URL has the wrong credentials.",
                    affected_service=service,
                    confidence=0.9,
                    proposed_fix=f"Restore {service}'s DATABASE_URL and recreate the container.",
                    reasoning="The logs show a PostgreSQL authentication failure while "
                    "the container is up and serving, which separates this from a crash.",
                    scenario="bad_config",
                    actions=[("rollback_config", service, "credentials are wrong")],
                )

        # 3. Connection exhaustion. Note the asymmetry that makes this scenario
        #    hard: the services *reporting* the error are the victims.
        exhausted = [s for s, text in logs.items() if any(m in text for m in EXHAUSTION_MARKERS)]
        if exhausted:
            holder = self._connection_holder(evidence)
            return self._answer(
                root_cause="PostgreSQL has no connection slots left; "
                + (
                    f"{holder} is holding them and is the service at fault."
                    if holder
                    else "one client is holding them all."
                ),
                affected_service=holder or "postgres",
                confidence=0.8 if holder else 0.6,
                proposed_fix=(
                    f"Restart {holder} to drop the connections it is holding."
                    if holder
                    else "Identify the client holding connections in pg_stat_activity "
                    "and restart it."
                ),
                reasoning="Several services report connection-slot errors. Those are "
                "the victims: the service at fault is the one holding connections "
                "while reporting no errors of its own."
                + (f" Here that is {holder}." if holder else ""),
                scenario="db_exhaustion",
                actions=(
                    [("restart_container", holder, "it is holding every connection")]
                    if holder
                    else []
                ),
            )

        # 4. Latency localised to part of the system, with nothing failing.
        latency = _metric(evidence, "recent_average_latency_ms")
        slow = _slow_service(latency)
        errors = _metric(evidence, "errors")
        if slow:
            service, value = slow
            return self._answer(
                root_cause=f"{service} is responding slowly ({value:.0f} ms against a "
                f"normal few milliseconds), which backs up every caller.",
                affected_service=service,
                confidence=0.75,
                proposed_fix=f"Investigate {service}: it is serving correctly but "
                f"slowly. Clear any injected delay, then look at its dependencies.",
                reasoning="Latency is elevated on one service and on its callers, but "
                "not on its peers, and the error count is unchanged - so this is "
                "slowness, not failure.",
                scenario="slow_dependency",
                actions=[],
            )

        # 5. Memory climbing with nothing else wrong. Lowest confidence of the
        #    five: a single reading cannot distinguish a leak from a service
        #    that is simply busy. A trend over time would, and that is a Phase 3
        #    improvement once the monitor stores history.
        memory = _metric(evidence, "memory_mb")
        leaking = _outlier(memory, MEMORY_OUTLIER_RATIO, MEMORY_OUTLIER_FLOOR_MB)
        if leaking:
            service, value = leaking
            return self._answer(
                root_cause=f"{service} is using {value:.0f} MB, far above its peers, "
                f"with no change in errors or latency - consistent with a memory leak.",
                affected_service=service,
                confidence=0.6,
                proposed_fix=f"Restart {service} to reclaim the memory, then find the "
                f"unbounded allocation. A restart treats the symptom only.",
                reasoning="Resident memory is the only abnormal reading. Requests are "
                "still succeeding, so nothing has failed yet - which is what makes a "
                "leak easy to miss.",
                scenario="memory_leak",
                actions=[("restart_container", service, "reclaim leaked memory")],
            )

        # 6. Nothing matched. Say so plainly at low confidence rather than
        #    inventing a cause - a confidently wrong diagnosis is worse than an
        #    honest "I do not know", and Phase 3 branches on this.
        unhealthy = [s for s, v in errors.items() if v > 0]
        return self._answer(
            root_cause="No single root cause identified from the available evidence."
            + (f" Services reporting errors: {', '.join(sorted(unhealthy))}." if unhealthy else ""),
            affected_service=alert_service if alert_service in _KNOWN else "unknown",
            confidence=0.25,
            proposed_fix="Gather evidence over a longer window and escalate to a human "
            "if the symptoms persist.",
            reasoning="None of the known failure signatures matched: no container is "
            "down, no authentication or connection-slot errors appear in the logs, and "
            "latency and memory are within normal range of each other.",
            scenario=None,
            actions=[],
        )

    def _connection_holder(self, evidence: dict[str, Any]) -> str | None:
        """The service holding connections: errors of its own, but none.

        This is the one piece of reasoning that actually matters for the
        exhaustion scenario, so it is worth doing properly rather than guessing
        from the alert.
        """
        candidates: list[str] = []
        for entry in evidence.get("metrics") or []:
            if entry.get("unreachable"):
                continue
            service = str(entry.get("service", "unknown"))
            held = entry.get("held_db_connections")
            if isinstance(held, (int, float)) and held > 0:
                return service
            if entry.get("errors") == 0:
                candidates.append(service)
        # Exactly one service erroring-free among several erroring ones is a
        # strong hint; more than one is not, so say nothing.
        return candidates[0] if len(candidates) == 1 else None

    def _answer(
        self,
        *,
        root_cause: str,
        affected_service: str,
        confidence: float,
        proposed_fix: str,
        reasoning: str,
        scenario: str | None,
        actions: list[tuple[str, str | None, str]],
    ) -> dict[str, Any]:
        return {
            "root_cause": root_cause,
            "affected_service": affected_service if affected_service in _KNOWN else "unknown",
            "confidence": confidence,
            "proposed_fix": proposed_fix,
            "reasoning": reasoning,
            "scenario": scenario,
            "proposed_actions": [
                {"tool": tool, "target": target, "reason": reason}
                for tool, target, reason in actions
            ],
        }

    # --- postmortem --------------------------------------------------------
    def _postmortem(self, context: dict[str, Any]) -> dict[str, Any]:
        diagnosis = dict(context.get("diagnosis") or {})
        service = diagnosis.get("affected_service", "unknown")
        root_cause = diagnosis.get("root_cause", "not determined")
        fix = diagnosis.get("proposed_fix", "none proposed")
        confidence = diagnosis.get("confidence", 0.0)
        timeline = list(context.get("timeline") or [])

        steps = "\n".join(f"- {line}" for line in timeline) or "- (no steps recorded)"
        body = (
            f"## Summary\n\n{root_cause}\n\n"
            f"## Timeline\n\n{steps}\n\n"
            f"## Root cause\n\n{root_cause}\n\n"
            f"Affected service: **{service}**. Confidence: {confidence}.\n\n"
            f"## Actions taken\n\n"
            "None. The agent is in diagnosis-only mode (Phase 2); no remediation "
            "tool is reachable from the graph. The proposed fix was:\n\n"
            f"> {fix}\n\n"
            f"## Prevention\n\n"
            "- Alert on the signal that identifies the cause, not only on the symptom.\n"
            "- Record per-service metric history so trends, not snapshots, drive "
            "diagnosis.\n\n"
            "_This report was generated by the rule-based stub provider, not a "
            "language model._"
        )
        return {
            "summary": f"{service}: {str(root_cause)[:200]}",
            "body": body,
        }


#: Mirrors schemas.KnownService. Kept as a plain set so the stub never emits a
#: service name that would fail validation.
_KNOWN = frozenset({"orders", "payments", "inventory", "postgres", "unknown"})
