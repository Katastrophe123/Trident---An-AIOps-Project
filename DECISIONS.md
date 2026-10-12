# Decisions and trade-offs

Phase 1 is §1–22; Phase 2 is §23–33.

Every entry is a point where the plan was ambiguous, where it conflicted with
another instruction, or where the obvious implementation would have produced a
system the Phase 2 agent could not actually diagnose.

---

## 1. No git operations were performed

**Conflict.** The project plan's "Git and GitHub" section asks for a repository,
commits per milestone and a pushed branch. A later standing instruction says:
*"I do not want you to do anything with git right now. And do not push anything,
I will instruct you with git commands after all the three projects are
constructed."*

**Decision.** The standing instruction wins, because it is the more recent and
more specific. Nothing was initialised, committed, pushed, or configured. There
is therefore **no repository URL** in the Phase 1 report. The working tree is
clean and ready for whatever git sequence comes next; `.gitignore` is written
and excludes `.chaos/`, `.env` and the virtualenv.

---

## 2. The plan's architecture diagram was not in the source document

**Gap.** The plan refers to a system diagram that the markdown file does not
contain.

**Decision.** The architecture was reconstructed from the prose — orders calling
payments and inventory, all three sharing one PostgreSQL — and drawn as a Mermaid
diagram in `README.md`. If the original diagram shows something different, the
README diagram is the thing to correct.

---

## 3. Chaos is injected two different ways, on purpose

The five scenarios do not have one natural mechanism, so they use two:

| Scenario | Mechanism | Why |
|---|---|---|
| Crashed service | `docker compose stop` | The container genuinely has to not be running. Faking it in-process would leave a listener on the port, which is a different failure with different symptoms. |
| Bad config | `docker compose up --force-recreate` with an override env file | A wrong `DATABASE_URL` has to be present *at process start*. |
| Slow dependency | `POST /_chaos/latency` | Must change behaviour in a live process without a restart — a restart would clear the very metrics being observed. |
| Memory leak | `POST /_chaos/memory-leak` | Same: the RSS has to grow in the running process. |
| DB exhaustion | `POST /_chaos/db-exhaust` | Needs connections held open by a real client. |

**Trade-off.** Two mechanisms means two failure modes for the tooling, and the
in-process ones require a fault-injection API that no real service would expose
(gated behind `CHAOS_ENABLED`, which the tests cover). The alternative —
everything through the container layer — would have made "slow dependency"
indistinguishable from "restarted service".

---

## 4. `chaos.py` drives the Compose CLI, not the Docker SDK

**Decision.** `tools/compose.py` shells out to `docker compose`.

**Why.** Three reasons, in order of weight:

1. The RUNBOOK's manual fix for each scenario is a command a human can paste.
   If the script used the SDK, the runbook would be documenting something other
   than what the script does.
2. The bad-config scenario needs a container recreated with different
   environment. That is what `--force-recreate` is for; reimplementing it over
   the SDK means re-deriving Compose's config merge.
3. The CLI already resolves the active Docker context. This machine runs
   **Colima**, not Docker Desktop, and the CLI handles that with no
   configuration.

**Noted for Phase 2.** The *agent* will use the Docker SDK, as the plan
specifies, because it needs `get_container_status` and `restart_container` from
*inside* a container with the socket mounted. `docker>=7.1` is already a
dependency. (Verified: `docker.from_env()` does resolve the Colima context on
this machine — an earlier assumption that it would not was wrong.)

---

## 5. The web console is an addition beyond the plan

The plan's Phase 1 does not ask for a UI. One was built anyway
(`services/console`), because the request was explicitly for a frontend and
because chaos scenarios that nobody can *see* are hard to demo and hard to
debug.

Constraints it was built under:

* **No build step, no framework, no npm.** Three files: `index.html`,
  `styles.css`, `app.js`. Readable by someone new to frontend, which was the
  stated requirement.
* **The browser talks to one origin.** The console fans out to the three
  services server-side, so there is no CORS configuration.
* **A down service is data, not an error.** Every fan-out call catches its own
  failure and reports `unreachable`. A dashboard that returns 500 when one
  service is down is useless exactly when it is needed.

---

## 6. The console cannot touch containers

**Decision.** The two container-level scenarios have **no button**. The UI shows
the exact command to copy instead, with a one-line explanation.

**Why.** Container control needs the Docker socket mounted into the console, and
the plan reserves socket access for the agent service. Mounting it into a
browser-facing service to save a copy-paste is a bad trade.

---

## 7. A refusal is not an outage

This is the single most consequential decision in Phase 1, because the Phase 2
agent is scored on telling these apart.

* Payment declined (over threshold) → `402` from payments → order is
  **`rejected`**, and `POST /orders` returns **`201`**.
* Unknown SKU → `404` from inventory → order **`rejected`**, `201`.
* Insufficient stock → `409` → order **`rejected`**, `201`.
* Dependency **unreachable** → order **`failed`**, `POST /orders` returns
  **`502`**, and `downstream_errors` names the service.

Only `5xx` increments the error counter. If a declined payment counted as an
error, every demo would show a permanent ~4% error rate and an agent that opened
an incident for it would be right to.

The traffic generator reinforces this: **4% of generated orders are deliberately
unservable** (`UNSERVABLE_SHARE`). A baseline of legitimate 4xx is normal, and an
agent that flags it is wrong.

---

## 8. `/metrics` is in-process JSON, not Prometheus

**Decision.** `MetricsRegistry` counts requests, errors, latency and memory in
process memory and serves them as JSON.

**Why.** The plan asks for exactly four readings and for the agent to read them
as a tool. Prometheus plus an exporter plus a scrape config is three more moving
parts for no gain at this scale, and the agent would then have to speak PromQL.

**Trade-off, stated plainly:** metrics reset when a container restarts, and they
are per-process so they would not survive horizontal scaling. For a
single-replica demo whose scenarios include "restart the container as the fix",
that is acceptable and occasionally useful.

Two latency figures are reported, deliberately:

* `average_latency_ms` — lifetime. Stable, but after thousands of fast requests
  a genuine spike barely moves it.
* `recent_average_latency_ms` and `recent_p95_latency_ms` — a bounded window of
  the last 200 requests. This is what actually moves when a dependency slows
  down, and it is what the console charts.

---

## 9. The injected delay is inside the latency timer

A subtle one, found by a failing test. The middleware originally slept *before*
starting its timer, so a deliberately-slowed service reported **normal** latency
in its own `/metrics` while its caller looked broken.

**Decision.** The timer starts first; the sleep is measured. A slow service now
reports itself as slow, which is what makes the root cause point at the right
place.

---

## 10. `/_chaos/*` and `/health` are excluded from metrics

The console polls every service every 2 seconds. Counting those polls would
drown the real traffic signal — at 2 orders/second the dashboard's own requests
would be a third of all traffic.

Exclusion is by **prefix**, not exact match. An exact-match list was the first
attempt and it had a real bug: `POST /_chaos/reset?reset_metrics=true` was itself
recorded *after* clearing the counters, so the metrics could never actually read
zero.

---

## 11. Logs go to stdout *and* a bounded in-memory ring buffer

`GET /_logs` serves the last N records (default 500) from a `deque(maxlen=...)`.

**Why.** The agent's Phase 2 `get_logs(service, minutes)` tool needs recent logs
over HTTP. The alternative is shelling out to `docker logs`, which needs the
Docker socket and returns unparsed text.

**Why bounded.** One of the five scenarios *is* a memory leak. An unbounded log
buffer would be a second, accidental leak, and the two would be impossible to
tell apart.

Third-party loggers (`httpx`, `httpcore`, `aiosqlite`) are pinned to `WARNING`.
They emit one unstructured line per call, duplicating the structured
`request_completed` record — and at a few orders a second they would crowd real
events out of a 500-entry buffer.

---

## 12. Postgres runs with `max_connections=25`

**Decision.** The database is started with a deliberately low connection limit,
and the SQLAlchemy pool is small (`pool_size=3`, `max_overflow=2`).

**Why.** The default is 100. Exhausting that from a demo script means holding
100+ connections and waiting, and the symptom arrives slowly or not at all. At
25, holding 20 connections makes the other services fail to reach the database
within seconds — which is the scenario the plan asks for.

---

## 13. The entrypoint does not exit when the database is unreachable

`docker/entrypoint.sh` waits for PostgreSQL but **does not** fail the container
when the wait times out.

**Why.** The bad-config scenario would otherwise become a crash loop, and its
symptom (`/health` reporting an authentication error while the container is up)
is precisely what distinguishes it from the crashed-service scenario. A container
restarting forever is a *different* incident with a different fix.

Refined once the scenario was actually run — see §18: it does not exit, but it no
longer *waits* either when the error is a credentials problem.

---

## 14. Exactly one service runs migrations

`orders` has `RUN_MIGRATIONS=true`; payments and inventory do not. Three services
racing `alembic upgrade head` against one database on startup is a real
intermittent failure, and it would be indistinguishable from an injected fault.

---

## 15. Status colours: three states, not four

The frontend's status palette was validated with a script rather than by eye. The
first attempt used four states (healthy / degraded / serious / unhealthy) and the
`warning`↔`serious` pair failed colour-vision-deficiency separation at ΔE 13.6
against an ΔE ≥ 15 floor.

**Decision.** `serious` was dropped. Three states — `healthy`, `degraded`,
`unhealthy` — re-validated at ΔE 27.6 for normal vision and 11.3 under CVD
simulation. Status is additionally never colour-alone: every pill carries an icon
and a word.

---

---

## 16. The services connect as a non-superuser role

**Decision.** `docker/initdb/01-app-role.sql` creates `aiops_app`
(`NOSUPERUSER`) and the three services connect as that, not as the
`POSTGRES_USER` superuser.

**Why.** Without it the database-exhaustion scenario cannot work at all.
PostgreSQL never enforces `max_connections` against superusers — they are always
admitted into the slots held back by `superuser_reserved_connections`. Observed:
the server was genuinely exhausted (`psql` itself reported `FATAL: sorry, too
many clients already`) while all three services opened fresh connections and
reported `healthy`. The fault was real and completely invisible.

A useful side effect, and how a real incident actually goes: an operator can
still get in with `psql` as the superuser to diagnose, because that is precisely
what the reserved slots are for.

---

## 17. Config overrides go through `docker compose --env-file`

**Decision.** `tools/compose.py` passes `--env-file .chaos/overrides.env` on
every compose invocation.

**Why.** The first attempt listed that file under each service's `env_file:`,
which does not work and took a failed injection to understand. `env_file`
injects variables **into the container**; the compose file resolves
`DATABASE_URL: ${PAYMENTS_DATABASE_URL:-<good>}` by **compose's own
interpolation**, which reads only the shell environment or `--env-file`. On top
of that, an explicit `environment:` entry always beats `env_file`. The symptom
was a service that came back perfectly healthy after a fault was supposedly
injected.

**Consequence worth knowing:** a plain `docker compose up` always gets the good
defaults, because it passes no `--env-file`. Only `tools/chaos.py` can inject
this fault. That is the behaviour worth having.

---

## 18. The entrypoint fails fast on credentials, slowly on cold starts

**Decision.** The DB wait loop retries a connection refusal up to 40 times, but
treats `password authentication failed` (and `role "..."`, `database "..."`,
`no pg_hba.conf entry`) as fatal and starts the service immediately.

**Why.** Retrying a wrong password for 40 seconds made the bad-config scenario
*look like a crashed service* for its first minute — a service that is not
answering at all. That blurs the one distinction scenario 2 exists to teach: the
container is **up** and `/health` reports the real reason. A cold start deserves
patience; a typo in a password never resolves itself.

---

## 19. Database exhaustion needs eviction, and a second acquisition pass

**Decision.** `exhaust_connections` takes every free slot, terminates the other
backends belonging to the same role, then **takes the slots that eviction just
freed**.

**Why.** Holding connections alone does nothing, for two separate reasons:

1. Every service's pool is already warm and simply reuses what it holds. An
   exhausted database only hurts something that needs an *additional*
   connection, and at two orders a second nothing ever does.
2. `pool_recycle` does not change that: SQLAlchemy recycles by closing the old
   connection *before* opening its replacement, so the replacement always fits
   into the slot just freed. (It was kept anyway — it is good practice.)

And the eviction step alone cancels itself out: terminating three service
backends frees exactly three slots, which the services reconnect straight into.
Hence the second pass. Terminating backends is also what a real exhaustion
incident does to you, so the mechanism is faithful rather than merely effective.

Full sequence of four failed attempts is in `RUNBOOK.md`, scenario 5.

---

## 20. Inventory replenishes depleted stock

**Decision.** A background task tops any SKU below 20% of its seeded level back
up every 20 seconds, clearing that SKU's reservations as shipped.

**Why.** Nothing in the demo ever *ships* an order, so every confirmed order
moves a unit from `available` to `reserved` permanently. 2500 seeded units at
roughly four units a second empties the warehouse in about ten minutes — after
which every order is a 409 `rejected` and **orders stops calling payments
altogether**. Three of the five scenarios then have nothing to show. Observed on
a stack that had been up for forty minutes:

```
  payments   HEALTHY   req=0   err=0
  orders by status: {'rejected': 1810, 'confirmed': 1254}
```

This does not weaken the deliberate-refusal baseline: the traffic generator's
unservable orders are an unknown SKU (404) and an over-limit quantity (payment
402), neither of which depends on stock levels.

---

## 21. `uvicorn.access` is suppressed

**Decision.** `uvicorn.access` is pinned to `WARNING`, alongside `httpx`,
`httpcore` and `aiosqlite`.

**Why.** It is not third-party noise, it is a *duplicate*. It emits one
unstructured line per request with no request id:

```json
{"event": "172.18.0.1:42484 - \"GET /orders/summary HTTP/1.1\" 200",
 "request_id": null, "logger": "uvicorn.access"}
```

while the middleware already emits `request_completed` carrying method, path,
status and latency as real fields, correlated by request id. Keeping both halved
the useful capacity of a 500-entry ring buffer and handed the agent two records
per request, one of them uncorrelatable. What is left is a clean trace per order:

```json
{"event": "downstream_ok", "downstream": "inventory", "path": "/reservations", "latency_ms": 11.23}
{"event": "downstream_ok", "downstream": "payments",  "path": "/payments",     "latency_ms": 6.31}
{"event": "order_confirmed"}
{"event": "request_completed", "path": "/orders", "status_code": 201, "latency_ms": 20.89}
```

all four sharing one `request_id`.

---

## 22. Coverage needs `concurrency = ["greenlet", "thread"]`

A measurement bug, not a design decision, but worth recording. SQLAlchemy's
asyncio layer runs the ORM inside greenlets, and coverage.py loses its trace
function across a greenlet switch — so every line after the first
`await session.get(...)` was reported as unexecuted even though passing tests
clearly ran it. `services/inventory/main.py` read 65% when it was really 90%, and
the project total understated by about ten points.

---

---

# Phase 2 — the agent

## 23. Phase 2 stops at diagnosis

**Decision.** The graph is linear — `gather_evidence → diagnose → postmortem` —
and no remediation tool is reachable from any node.

**Why.** The plan's build order says so explicitly: "Build the tools, then a
linear graph (gather → diagnose → postmortem) with no fixing yet. Get the
diagnosis right before the agent is allowed to act." Deferred to Phase 3:
evidence gathering as three parallel nodes, the approval interrupt,
apply-fix/verify, the retry cap, escalation and checkpointing.

Two pieces of Phase 3 are *measured* already so the branch can be validated
before it exists: `needs_more_evidence` (the low-confidence condition) is
computed and recorded on every diagnosis, and `AgentState` declares
`approval_status`, `attempts` and `verification`. Adding a field to a state
object that is already being checkpointed is more disruptive than leaving room.

---

## 24. Tools are an allowlist, and the risky ones are on a different class

**Decision.** Five named tools in a registry, each with a `risk` level. The
three read tools are reachable from the graph; `restart_container` and
`rollback_config` live on a separate `RemediationTools` class that no node
imports.

**Why.** "The LLM never runs arbitrary shell commands" has to be a property of
the code, not a line in a prompt. The model's output is *data*, validated
against a Pydantic schema; a `proposed_action` naming anything outside the
registry is dropped with a warning at the diagnose node. An unknown tool name
is treated as **risky**, not safe — fail closed.

The alternative, a shell plus an instruction to be careful, fails the first time
a log line reads like an instruction. An allowlist cannot be argued out of its
own contents. `GET /tools` publishes the list so the restriction is inspectable.

---

## 25. The stub provider is a baseline, not a mock

**Decision.** With no `LLM_API_KEY` the agent runs a deterministic, rule-based
diagnoser and labels everything `source: "stub"` — in the API, the stored
incident, the postmortem body, and on `/health` as a `degraded` dependency.

**Why three things at once.** It keeps the test suite offline (a suite that
needs an API key does not run in CI); it lets the graph be demonstrated with no
account anywhere; and it is the **baseline** the Phase 4 evaluation needs.
"The agent diagnosed 5/5" is close to meaningless next to "a page of
if-statements also diagnosed 5/5". If a real model cannot beat it, that is the
finding.

**The risk, and the mitigation.** A good stub could be mistaken for a working
model. Hence the labelling in four places, and a test asserting the postmortem
says so.

---

## 26. The five scenarios are described in the prompt

**Decision.** `DIAGNOSE_SYSTEM` names and characterises all five failure modes.

**Why.** This is a closed-world diagnosis task with five known root causes.
Withholding them to see whether a model can infer the taxonomy from first
principles is a different and harder experiment, and it would measure the wrong
thing — a real on-call engineer has a runbook. Phase 4 scores whether the agent
picks the *right* scenario, not whether it can invent the list.

The prompt also states the two mistakes that make a diagnosis useless: the
service reporting errors is not always the service at fault, and a 4xx refusal
is not an outage.

---

## 27. A transitional container is not a crashed one

**Found by running it.** Injecting bad-config recreates the container, so the
monitor fired while it was mid-recreate and the agent diagnosed
`crashed_service` at **0.95 confidence** — correct for that instant, and
completely wrong about the fault underneath.

**Decision.** `exited`/`dead` are a crash (`not_running`); `created`,
`restarting`, `paused`, `removing` are a transition (`transitioning`), reported
separately. A transitional container alone yields confidence 0.3 and "the system
is still settling", with no proposed action. An unrecognised state is treated as
stopped, so nothing disappears from the evidence.

Both the prompt and the stub enforce it. After the fix the same injection
diagnoses `bad_config` at 0.90.

---

## 28. Recovery resolves an incident

**Found by running it.** The monitor skips a service that already has a live
incident, so one outage produces one investigation. But Phase 2 applies no fix
and therefore never resolved anything — so the *first* incident for a service
silenced the monitor for that service permanently. The second scenario run
produced no new incident at all.

**Decision.** When a service's health returns, any live incident for it is
marked `resolved` with a `resolved_at`. Recovery is the only resolution signal
an agent that cannot act actually has: "the symptom stopped" is all it can
observe. The diagnosis and postmortem are kept.

---

## 29. The agent connects as the superuser

**Found by running it, and the most interesting failure of the phase.** During
database exhaustion the agent is *also a victim*: it shares one PostgreSQL with
the services it watches, so it was refused connections too. Its `/health`
returned 503, `GET /incidents` returned 500, and it could not open an incident
for the one fault it most needed to diagnose.

**Decision, two parts.**

1. The agent connects as `aiops` (superuser), not `aiops_app` like every other
   service. PostgreSQL holds `superuser_reserved_connections` back for exactly
   this situation — so an operator can get in when the application has taken
   everything. The agent *is* the operator, so it reaches for those slots the
   same way a human reaches for psql.
2. Persistence is best-effort at both ends. If the incident cannot be opened or
   saved, the investigation still runs and the conclusion is logged, so a
   diagnosis reached while storage was down is recoverable.

**The trade-off, stated.** More database privilege than the agent needs, in
exchange for working during the scenario where it is also a victim. A production
design would give the agent its own database rather than a bigger grant. The
general lesson is the one worth keeping: **observability tooling must not share
a failure domain with the thing it observes.**

---

## 30. `degraded` does not raise an alert

**Decision.** The monitor alerts on `unhealthy` and `unreachable` only.

**Why.** Slow dependency and memory leak both report `degraded` while still
serving correctly. Alerting on degraded would open an incident the instant a
fault is injected — before there is any symptom to diagnose, since the chaos
endpoints set the flag immediately but latency and memory take tens of seconds
to move. Those two scenarios are therefore triggered manually, which is
documented rather than hidden.

---

## 31. Persistence happens after the graph, not inside the nodes

**Decision.** Nodes take state and return state; nothing in them knows about a
database session. `IncidentRepository` writes once, after the graph finishes.

**Why.** It is what makes the nodes testable without a database, and it keeps
the graph pure. **The trade-off is explicit:** if the process dies
mid-investigation, that investigation leaves no trace. Phase 3's checkpointer
fixes that, and until the graph can be interrupted there is nothing to resume
anyway — which is also why Phase 2 compiles the graph with no checkpointer at
all rather than adding a PostgreSQL dependency for no benefit.

---

## 32. Smaller things found while building Phase 2

* **A degraded dependency now degrades the service.** `/health` only looked for
  `unhealthy` dependencies, so the agent reported `healthy` while its own `llm`
  dependency said `degraded`. Anything a dependency reports has to be reachable
  from the status field the monitor reads.
* **The CI workflow was invalid YAML and had never been parsed.**
  `DATABASE_URL: sqlite+aiosqlite:///:memory:` — a plain scalar ending in `:` is
  read as a mapping key. It would simply have failed on GitHub. There is now a
  test that parses both the workflow and the compose file.
* **`import docker` resolves to this repository's own `docker/` directory**
  under mypy, because it is a namespace package with no Python in it. Isolated
  to one helper with one suppression rather than fought with config; disabling
  namespace packages globally breaks langgraph, which legitimately is one.
* **The monitor tests raced the wall clock** and failed under coverage. Fixed by
  extracting `monitor_once`, so a polling pass is tested by calling it rather
  than by starting a task and sleeping long enough to hope.
* **The stub's latency rule was wrong on realistic data.** Comparing the slowest
  service against the median of the others cancels out in exactly the case that
  matters: in a real slow-dependency incident *two* services are slow — the slow
  one and the caller waiting on it. Replaced with an absolute floor plus a
  spread check, preferring a callee over the caller.

---

## 33. Known limitations

Phase 1 limitations, stated rather than hidden (Phase 2's are in §23–32 and in
the README):

* **The memory leak is reclaimed on `reset`.** A real leak is not. `reset` frees
  the leaked chunks so a demo can be re-run without a restart; the *honest* fix,
  and the one the RUNBOOK documents, is `docker compose restart <service>`.
* **No authentication anywhere.** The plan does not ask for it in Phase 1 and the
  stack binds to localhost. `/_chaos` is gated by `CHAOS_ENABLED`, which is a
  demo switch, not a security boundary.
* **Metrics and logs are per-process and in-memory.** See §8 and §11.
* **Container-level scenarios are not covered by automated tests.** Faking
  `docker compose stop` would only test the mock. They are verified by hand and
  the walkthrough is recorded in `RUNBOOK.md`.
* **One replica per service.** Nothing here would survive scaling out, and
  nothing here needs to.
