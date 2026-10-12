"""Prompts for the diagnose and postmortem nodes.

Kept in their own module because they are the part most likely to change, and
because a prompt buried inside a graph node is a prompt nobody reviews.

Two choices worth defending:

* **The five known scenarios are described in the system prompt.** This is a
  closed-world diagnosis task: the demo system has exactly five failure modes
  and each has one root cause. Withholding that to see whether the model can
  infer them from first principles would be a different, harder experiment -
  and would measure the wrong thing, since a real on-call engineer has a
  runbook. The Phase 4 evaluation scores whether the agent picks the *right*
  scenario, not whether it can invent the taxonomy.
* **The distinction that actually matters is stated explicitly**: the service
  reporting errors is not always the service at fault, and a 4xx refusal is not
  an outage. Those are the two mistakes that make a diagnosis useless, and both
  are easy for a model to make from a wall of logs.
"""

from __future__ import annotations

import json
from typing import Any

DIAGNOSE_SYSTEM = """\
You are an on-call site reliability engineer diagnosing an incident in a small \
order-processing system. You are given evidence that has already been \
collected; you cannot run commands.

The system has three services behind one shared PostgreSQL database:
- orders: receives orders, calls inventory to reserve stock, then payments to charge
- payments: charges fake payments, declines any amount over 9000
- inventory: reserves stock, 404s an unknown SKU, 409s insufficient stock

It has exactly FIVE known failure modes. Identify which one is happening:

1. crashed_service - a container is not running. Callers get connection errors
   ("name or service not known", "connection refused"). Docker reports the
   container as exited. Fix: restart the container.
2. bad_config - a service has the wrong database password. The container is UP
   and serving, but its /health returns 503 and its logs show "password
   authentication failed". Fix: restore the config and recreate the container.
3. slow_dependency - one service answers correctly but slowly. Its own latency
   and its callers' latency both climb; error counts do NOT rise. Fix:
   investigate that service.
4. memory_leak - one service's memory climbs steadily while requests, errors
   and latency stay normal. Nothing has failed yet. Fix: restart it, then find
   the unbounded allocation.
5. db_exhaustion - one service holds every PostgreSQL connection. The OTHER
   services report "too many clients"/"remaining connection slots" errors and
   go unhealthy. Fix: restart the service that is holding the connections.

Three rules you must not get wrong:

RULE 1 - The service reporting errors is not always the service at fault.
In db_exhaustion the culprit reports ZERO errors of its own while its victims
report many. Name the culprit in `affected_service`, not the loudest victim.

RULE 2 - A container in a transitional state is not a crash.
Docker states `created`, `restarting` and `paused` mean the container is being
started or replaced right now. Only `exited` and `dead` are a crash. A deploy in
progress looks identical to an outage from the caller's side, so if the only
abnormal signal is a transitional container, return a LOW confidence and say the
system is still settling.

RULE 3 - A refusal is not an outage.
This system deliberately refuses about 4% of orders: an unknown SKU gives 404,
an over-limit amount gives 402, insufficient stock gives 409. Those produce
"rejected" orders and are NORMAL. Only 5xx responses and unreachable
dependencies are failures. A steady trickle of 4xx is not an incident.

If the evidence does not clearly match one scenario, say so and return a LOW
confidence (below 0.4). A confidently wrong diagnosis is worse than an honest
"insufficient evidence".

Reply with ONLY a single JSON object, no prose and no markdown fences:

{
  "root_cause": "<one or two sentences>",
  "affected_service": "orders" | "payments" | "inventory" | "postgres" | "unknown",
  "confidence": <float 0.0-1.0>,
  "proposed_fix": "<what a human should do>",
  "reasoning": "<which evidence led you here, and what you ruled out>",
  "scenario": "crashed_service" | "bad_config" | "slow_dependency" | "memory_leak" | "db_exhaustion" | null,
  "proposed_actions": [{"tool": "restart_container"|"rollback_config", "target": "<service>", "reason": "<why>"}]
}

`proposed_actions` may only name those two tools. Leave it empty if no action
is warranted.\
"""

POSTMORTEM_SYSTEM = """\
You are an SRE writing a short, factual incident postmortem. Be concise and \
concrete. Do not invent events that are not in the timeline or evidence. Do not \
assign blame to people.

Reply with ONLY a single JSON object, no prose and no markdown fences:

{
  "summary": "<one line, under 200 characters>",
  "body": "<markdown with these sections: ## Summary, ## Timeline, ## Root cause, ## Actions taken, ## Prevention>"
}

Under "Actions taken", state plainly that the agent is in diagnosis-only mode \
and applied no remediation; report the proposed fix as a recommendation.\
"""


def _trim_records(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keep the fields that carry diagnostic signal, drop the rest.

    Raw log records carry timestamps, logger names and request ids that cost
    tokens and add nothing to a diagnosis. Errors and warnings are kept ahead
    of info lines because a 40-record window during an incident is mostly
    routine traffic.
    """
    keep = (
        "level",
        "event",
        "error",
        "reason",
        "status_code",
        "latency_ms",
        "downstream",
        "failure_kind",
        "stage",
        "path",
        "detail",
    )

    def rank(record: dict[str, Any]) -> int:
        return {"ERROR": 0, "WARNING": 1}.get(str(record.get("level")), 2)

    ordered = sorted(records, key=rank)[:limit]
    return [{k: v for k, v in record.items() if k in keep and v is not None} for record in ordered]


def render_evidence(evidence: Any, *, log_limit: int = 12) -> dict[str, Any]:
    """Turn the Evidence object into a compact dict for the prompt."""
    payload = (
        evidence.model_dump(mode="json") if hasattr(evidence, "model_dump") else dict(evidence)
    )

    for entry in payload.get("logs") or []:
        entry["records"] = _trim_records(list(entry.get("records") or []), log_limit)

    # Container listings include the agent itself and the traffic generator,
    # which are noise for a diagnosis about the three demo services.
    containers = payload.get("containers") or {}
    containers["containers"] = [
        {
            k: v
            for k, v in item.items()
            if k in ("service", "state", "health", "restart_count", "exit_code")
        }
        for item in (containers.get("containers") or [])
    ]
    payload["containers"] = containers
    return payload


def diagnose_user_prompt(
    *, alert_service: str, trigger: str, evidence: Any, alert_detail: Any = None
) -> str:
    """The user half of the diagnose request."""
    rendered = render_evidence(evidence)
    sections = [
        "## Alert",
        f"- service: {alert_service}",
        f"- trigger: {trigger}",
    ]
    if alert_detail:
        sections.append(f"- health body: {json.dumps(alert_detail)[:800]}")

    sections += [
        "",
        "## Container status",
        json.dumps(rendered.get("containers", {}), indent=2)[:2500],
        "",
        "## Metrics (per service)",
        json.dumps(rendered.get("metrics", []), indent=2)[:3500],
        "",
        "## Recent logs (errors and warnings first)",
        json.dumps(rendered.get("logs", []), indent=2)[:6000],
    ]
    failures = rendered.get("failures") or {}
    if failures:
        sections += [
            "",
            "## Evidence that could not be collected",
            json.dumps(failures, indent=2)[:800],
            "(Account for these gaps in your confidence.)",
        ]
    sections += ["", "Diagnose the incident. Reply with the JSON object only."]
    return "\n".join(sections)


def postmortem_user_prompt(*, diagnosis: Any, timeline: list[str], alert_service: str) -> str:
    """The user half of the postmortem request."""
    payload = (
        diagnosis.model_dump(mode="json") if hasattr(diagnosis, "model_dump") else dict(diagnosis)
    )
    return "\n".join(
        [
            f"## Incident on {alert_service}",
            "",
            "## Timeline",
            *(f"- {line}" for line in timeline),
            "",
            "## Diagnosis",
            json.dumps(payload, indent=2)[:3000],
            "",
            "Write the postmortem. Reply with the JSON object only.",
        ]
    )
