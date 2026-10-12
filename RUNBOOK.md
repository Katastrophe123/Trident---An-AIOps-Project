# Runbook — the five failure scenarios

One page per scenario: how to inject it, what it actually looks like in logs and
metrics, and how to fix it by hand.

**Every output below was copied from a real run**, not written from intention.
Three of the five needed code changes before they produced the symptom they were
supposed to; those are recorded in the "what went wrong building this" notes,
because the wrong behaviour is instructive.

## Before you start

```bash
docker compose up -d --build --wait     # the stack, plus a traffic generator
python -m tools.chaos status            # should print HEALTHY three times
```

Traffic matters. Without it a broken service produces no errors and no latency,
so there is nothing to observe. The `traffic` container sends ~2 orders/second;
stop it with `docker compose stop traffic` if you want a quiet system.

A healthy baseline looks like this:

```
Demo system status
  orders     HEALTHY       req=56     err=0     lat=   30.2ms mem=  83.1MB
  payments   HEALTHY       req=56     err=0     lat=    4.7ms mem=  78.1MB
  inventory  HEALTHY       req=59     err=0     lat=   11.8ms mem=  78.5MB

  orders by status: {'confirmed': 53, 'rejected': 3}
```

**Note the `rejected` count in a healthy system.** 4% of generated orders are
deliberately unservable — an unknown SKU, or a quantity over the payment limit.
Those are 4xx refusals and `rejected` orders, and they are *not* an incident.
Anything that treats them as one is wrong.

### The one-line summary of the whole thing

| Reading | Crash | Bad config | Slow dep. | Memory leak | DB exhaustion |
|---|---|---|---|---|---|
| Container | **Exited** | Up | Up | Up | Up |
| Its own `/health` | no answer | **503** | 200 degraded | 200 degraded | **503** |
| Its own `errors` | — | 0 | **0** | **0** | **0** |
| Its own latency | — | normal | **~3000ms** | normal | normal |
| Its own `memory_mb` | — | normal | normal | **climbing** | normal |
| *Other* services | degraded | degraded | slow | fine | **unhealthy, erroring** |

The last column is the hard one: **the service at fault reports no errors at all,
and the two innocent services are the ones failing.**

---

## Scenario 1 — Crashed service

**Root cause:** the container is not running.

### Inject

```bash
python -m tools.chaos crash --service payments
# or, equivalently:  docker compose stop payments
```

### Symptoms

`docker compose ps -a` — the giveaway that separates this from scenario 2:

```
payments	Exited (0) 30 seconds ago
```

`curl -s localhost:8001/health` — orders is **degraded**, not unhealthy. It can
still serve; one dependency cannot be reached:

```
status: degraded | detail: a dependency is unavailable
  postgres  -> healthy
  payments  -> unhealthy   ConnectError: [Errno -2] Name or service not known
  inventory -> healthy
```

`curl -s localhost:8001/metrics`:

```
requests 125 | errors 39 | error_rate 0.312
downstream_errors {'payments': 39}
status_counts {'2xx': 86, '5xx': 39}
```

`downstream_errors` names the offender directly. Order outcomes:

```
{"confirmed":233,"failed":39,"rejected":14}
```

**`failed`, not `rejected`** — this is the distinction the whole system is built
around. A refusal is a 4xx and a `rejected` order; an unreachable dependency is
a 502 and a `failed` order.

`curl -s 'localhost:8001/_logs?limit=5&level=ERROR'`:

```json
{
  "level": "ERROR",
  "event": "order_failed",
  "request_id": "fbfbc151a1b04cf0a202d98d30f89dc4",
  "order_id": "57a68207-a10f-4237-8e8a-7ea50b4ab57f",
  "stage": "take_payment",
  "downstream": "payments",
  "failure_kind": "unavailable",
  "reason": "payments unavailable: [Errno -2] Name or service not known"
}
```

`failure_kind: "unavailable"` and `stage: "take_payment"` say what broke and
where. The console shows payments as `unreachable` while the other two stay as
they were.

### Fix

```bash
docker compose start payments
```

Recovery takes about 20 seconds (DB wait plus the health check). The error
counters do **not** reset — they are lifetime counts — but `recent_average_latency_ms`
and `/health` return to normal, and new orders are `confirmed` again.

---

## Scenario 2 — Bad config

**Root cause:** `DATABASE_URL` has the wrong password.

### Inject

```bash
python -m tools.chaos bad-config --service payments
```

This writes `PAYMENTS_DATABASE_URL` into `.chaos/overrides.env` and recreates
just that container with it.

### Symptoms

**The container is up.** That one fact separates this from a crash:

```
payments	Up 11 seconds (health: starting)
```

`curl -i localhost:8002/health` → **HTTP 503**, and the body says exactly why:

```
status: unhealthy
detail: DBAPIError: (sqlalchemy.dialects.postgresql.asyncpg.Error)
        password authentication failed for user "aiops_app"
  postgres -> unhealthy | password authentication failed for user "aiops_app"
```

`curl -s 'localhost:8002/_logs?level=ERROR'`:

```json
{
  "level": "ERROR",
  "event": "database_healthcheck_failed",
  "error": "DBAPIError: (...asyncpg.Error) password authentication failed for user \"aiops_app\""
}
```

From orders' point of view the difference from scenario 1 is visible in one
field — `HTTP 503` instead of a connect error:

```
orders status: degraded
  postgres  -> healthy
  payments  -> unhealthy | HTTP 503
  inventory -> healthy
```

```
errors 24 | downstream_errors {'payments': 24} | status_counts {'2xx': 3, '5xx': 24}
```

So: **crash ⇒ `ConnectError` and an exited container. Bad config ⇒ `HTTP 503`, a
running container, and a credentials error in the logs.**

### Fix

```bash
python -m tools.chaos reset
```

By hand: delete the line from `.chaos/overrides.env` and
`docker compose up -d --force-recreate --no-deps payments`.

### What went wrong building this

Two bugs, both of which made the injection silently do nothing:

1. **The override never reached the service.** The compose file resolves
   `DATABASE_URL: ${PAYMENTS_DATABASE_URL:-<good default>}`, and that
   interpolation is done *by compose itself*, which reads only the shell
   environment or `--env-file`. The first attempt listed the override file under
   the service's `env_file:` — which injects the variable *into the container*,
   where nothing reads it, and loses to the explicit `environment:` entry
   anyway. Proof, from the running container:

   ```
   DATABASE_URL=postgresql+asyncpg://aiops_app:aiops@db:5432/aiops          <- still good
   PAYMENTS_DATABASE_URL=postgresql+asyncpg://aiops_app:definitely-not...   <- read by nobody
   ```

   Fixed by passing `docker compose --env-file .chaos/overrides.env`.

2. **It then looked like a crash for its first minute.** The entrypoint retried
   the database 40 times at one second each, so the service did not start for
   ~60s — indistinguishable from scenario 1. The entrypoint now treats
   `password authentication failed` (and friends) as fatal and starts
   immediately, because retrying a wrong password cannot help. The whole point
   of this scenario is a container that is **up** and reporting the real reason.

The script's own guard caught the first bug by noticing the service came back
`healthy` after a fault was supposedly injected:

```
! Expected unhealthy. The override may not have been picked up - check that
  docker-compose.yml lists .chaos/overrides.env in env_file.
```

---

## Scenario 3 — Slow dependency

**Root cause:** one service is slow. Nothing is failing.

### Inject

```bash
python -m tools.chaos slow-dependency --service inventory --seconds 3
```

### Symptoms

Before:

```
orders    recent_avg =   29.2ms   p95 =   38.1ms
inventory recent_avg =   11.6ms   p95 =   17.3ms
```

After ~90 seconds of traffic:

```
  orders     recent_avg=  3086.3ms  p95=  3058.3ms  errors=0
  payments   recent_avg=     5.6ms  p95=     8.8ms  errors=0
  inventory  recent_avg=  3020.9ms  p95=  3034.9ms  errors=0
```

Three things to read off that:

* **`errors=0` everywhere.** Slow is not failing. Nothing 5xxs, no order is
  `failed`. An agent that opens an incident for *errors* here finds nothing.
* **inventory reports itself as slow** (3020ms), not just its caller. That is
  what points the diagnosis at the right service.
* **payments is untouched** (5.6ms), which rules out "the database is slow" and
  "the host is overloaded".

orders' own `/health` localises it to one dependency:

```
  postgres  -> healthy | latency_ms: 2.4
  payments  -> healthy | latency_ms: 7.82
  inventory -> healthy | latency_ms: 3020.08
```

And the per-call log line names the exact call:

```json
{"event": "downstream_ok", "downstream": "inventory",
 "path": "/reservations", "latency_ms": 3031.51}
```

`downstream_ok`, not `downstream_failed` — it succeeded, slowly.

The slowed service reports `degraded`, with 200 rather than 503, because it is
still answering correctly:

```
  inventory  DEGRADED   req=137  err=0  lat= 2119.6ms  [delay 3000ms]
             detail: fault injection is active in this process
```

### Fix

```bash
python -m tools.chaos slow-dependency --service inventory --seconds 0
```

Latency returns to normal within one `recent` window (200 requests).

### What went wrong building this

The middleware originally slept **before** starting its latency timer, so the
injected delay was not measured: inventory reported a perfectly normal ~11ms
while orders looked broken at 3000ms. That points the diagnosis at the wrong
service — the caller, not the culprit. The timer now starts first. A unit test
pins it (`test_injected_latency_shows_up_in_the_measured_latency`).

---

## Scenario 4 — Memory leak

**Root cause:** unbounded growth inside the process. The hardest scenario to
notice, because nothing is failing *yet*.

### Inject

```bash
python -m tools.chaos memory-leak --service orders
```

A background task appends a 4 MiB chunk every 0.5s, with a 1 GiB ceiling so a
forgotten leak cannot take the machine down.

### Symptoms

```
orders memory before: 84.13 MB

  t+ 12s  memory=  184.1MB  requests=357   errors=0
  t+ 24s  memory=  280.1MB  requests=380   errors=0
  t+ 36s  memory=  372.1MB  requests=404   errors=0
  t+ 48s  memory=  468.1MB  requests=468   errors=0
  t+ 60s  memory=  564.1MB  requests=452   errors=0
```

Monotonic, roughly 8 MB/s, and **every other reading is normal**: requests keep
succeeding, `errors` stays 0, latency is unchanged. `memory_mb` is the only
signal. This is the scenario where waiting for errors means waiting for an OOM
kill.

```json
{"level": "WARNING", "event": "chaos_memory_leak_growing",
 "chaos_scenario": "memory_leak", "leaked_mb": 572}
```

`/health` reports `degraded` with **HTTP 200** — it is still serving correctly:

```
degraded | fault injection is active in this process
```

### Fix

```bash
docker compose restart orders
```

```
memory after restart: 82.94 MB | requests reset to 45
chaos state healthy: True
```

**Restart, not "stop the leak".** A real leak is not reclaimed by stopping the
code that leaked — the memory is still allocated. `chaos.py reset` *does* free it
(`stop_memory_leak(free=True)`) so a demo can be re-run without a restart, but
that is a convenience, not the honest fix. Note also that restarting resets the
in-process metrics; see `DECISIONS.md` §8.

---

## Scenario 5 — Database connection exhaustion

**Root cause:** one service holds connections and never returns them.

**This is the interesting one: the service at fault reports no errors, and the
other two are the ones that fail.**

### Inject

```bash
python -m tools.chaos db-exhaustion --service payments --connections 20
```

```
✓ payments is holding 22 connections
```

### Symptoms

All three services go **unhealthy**, with the same error:

```
  orders     UNHEALTHY     req=48   err=25   lat=  35.8ms  mem= 83.4MB
             detail: remaining connection slots are reserved for roles with the SUPERUSER attribute
  payments   UNHEALTHY     req=22   err=0    lat=   5.0ms  mem= 79.0MB  [22 held conns]
             detail: remaining connection slots are reserved for roles with the SUPERUSER attribute
  inventory  UNHEALTHY     req=48   err=25   lat=  12.4ms  mem= 83.5MB
             detail: remaining connection slots are reserved for roles with the SUPERUSER attribute
```

`/health` is **503 on all three**. orders is failing most of its traffic:

```
errors 54 | error_rate 0.7013 | status_counts {'2xx': 23, '5xx': 54}
```

Now read the error columns again:

```
  orders     err=25
  payments   err=0     <- the service at fault
  inventory  err=25
```

**payments, the culprit, has zero errors.** It is not making new connections — it
is sitting on the ones it took. Blaming the services that are *reporting* errors
gets the wrong answer, and both of them would be "fixed" by a restart that
changes nothing.

The one reading that identifies the culprit:

```
  orders     held_db_connections=0
  payments   held_db_connections=22    <-
  inventory  held_db_connections=0
```

Confirmed server-side (as the superuser, which can still get in — that is what
the reserved slots are for):

```
$ docker compose exec db psql -U aiops -d aiops \
    -c "SELECT usename, count(*) FROM pg_stat_activity WHERE datname='aiops' GROUP BY 1;"

  usename  | count
-----------+-------
 aiops     |     1
 aiops_app |    22
```

In a real incident the equivalent is grouping `pg_stat_activity` by
`client_addr` or `application_name` and seeing one client holding everything.

orders' error log shows the failure reaching the business layer, not just health:

```
  request_completed            | 502
  order_persist_failed         |
  downstream_failed            |
  database_healthcheck_failed  | remaining connection slots are reserved for roles with the SUPERUSER attribute
```

### Fix

```bash
python -m tools.chaos db-exhaustion --service payments --connections 0
```

Recovery is complete within ~20s:

```
  orders     HEALTHY   req=127  err=63  lat= 35.5ms  mem= 87.0MB
  payments   HEALTHY   req=63   err=0   lat=  5.1ms  mem= 79.1MB
  inventory  HEALTHY   req=127  err=62  lat= 12.3ms  mem= 85.4MB
```

(The error counts are lifetime totals and stay.) By hand, without the script:
restart the offending service — `docker compose restart payments` — which drops
every connection it held.

### What went wrong building this

This scenario did not work for **four** attempts, and each failure was a
different piece of real behaviour:

1. **Postgres does not enforce `max_connections` against superusers.**
   `POSTGRES_USER=aiops` is a superuser, so it is always let into the slots held
   back by `superuser_reserved_connections`. The database was genuinely
   exhausted — `psql` itself reported `FATAL: sorry, too many clients already` —
   while all three services opened fresh connections and reported healthy. Fixed
   by adding a `NOSUPERUSER` role (`docker/initdb/01-app-role.sql`) for the
   services to connect as. A pleasant side effect: an operator can still get in
   with `psql` to diagnose, which is exactly what those reserved slots are for.

2. **Every pool was already warm.** Exhaustion only hurts something that needs a
   *new* connection, and at two orders a second with a three-connection pool,
   nothing ever did.

3. **`pool_recycle` did not fix that.** SQLAlchemy recycles by closing the old
   connection *before* opening its replacement, so the replacement always fits
   into the slot just freed. (`pool_recycle` was kept anyway — it is good
   practice, and it is why the short value is documented in `.env.example`.)

4. **Evicting the other backends cancelled itself out.** Terminating three
   service backends frees exactly three slots, and the services reconnect
   straight back into them. Logged at the time:

   ```json
   {"event": "chaos_db_connections_held", "opened": 19,
    "total_held": 19, "evicted_backends": 3}
   ```
   …and `pg_stat_activity` showed `aiops_app | 22` — 19 held plus the 3 that had
   just reconnected.

The working mechanism is therefore three steps: take every free slot, terminate
the other backends belonging to the same role, then **immediately take the slots
that eviction freed**. The eviction step is what a real exhaustion incident does
to you, and the second acquisition pass is what keeps it from undoing itself.

---

## Putting everything back

```bash
python -m tools.chaos reset
```

Which does, in order: clear `.chaos/overrides.env`; recreate orders, payments,
inventory and console; wait for each to answer; clear in-process faults and
reset their metrics and log buffers; restock inventory; re-check health.

```
Resetting to a healthy system
  → no config overrides were set
  → docker compose up -d --force-recreate --no-deps orders payments inventory console
  ✓ orders reset (clean)
  ✓ payments reset (clean)
  ✓ inventory reset (clean)
  ✓ inventory restocked

  ✓ every service reports healthy
```

Exit code is 0 when everything is healthy and 1 otherwise, so it can be used as
a gate in a script.

`reset` does **not** cover a container you stopped yourself outside the tool
(`docker compose start <service>` for that), and it does not clear the orders
table — order history is cumulative by design.

---

## Two things that look like faults and are not

**A steady trickle of `rejected` orders.** 4% of generated orders are
deliberately unservable. In a healthy system you will always see something like
`{'confirmed': 53, 'rejected': 3}`. That is the baseline, not a symptom.

**`stock_replenished` in inventory's logs.** Nothing in the demo ever *ships* an
order, so each confirmed order moves a unit from `available` to `reserved`
permanently. 2500 seeded units at roughly four units a second empties the
warehouse in about ten minutes, after which every order is a 409 and **orders
stops calling payments at all** — which silently removes the symptoms of three
of the five scenarios. A background task tops depleted SKUs back up every 20s
and logs it. If you see a stack where everything is `rejected` and payments has
`req=0`, that is what happened.

```
  payments   HEALTHY   req=0   err=0   lat= 0.0ms   mem= 77.6MB
  orders by status: {'rejected': 1810, 'confirmed': 1254}
```
