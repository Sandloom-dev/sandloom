---
search:
  exclude: true
---

# ADR-0005: One effect model — guarantees, the dedupe record and the commit pipeline

- **Status:** Accepted (amended 2026-10-24: durable HTTP guard, ambiguous
  COMMIT, guarantee windows, at-most-once wording — after the best-practice
  review `docs/design/effect-model-best-practices-2026-10-24.md`)
- **Date:** 2026-10-24
- **Deciders:** Sandloom maintainers
- **Supersedes:** — (consolidates the guarantee prose scattered over
  `AGENTS.md`, `docs/SERVICE_RPC.md` and the module docstrings of
  `sandloom.core.unit_of_work`, `sandloom.core.idempotency`, `sandloom.core.tasks.enqueue`)

## Context

Sandloom's write-side guarantees grew one mechanism at a time: the transactional
outbox and its relay, the durable consumer inbox, the cache consumer inbox,
the durable command log of command contracts (and its cache fallback), the
HTTP `idempotency_guard`, the task-queue enqueue dedupe, `on_commit`,
`before_commit` and `revocation_at_commit`. Each is sound on its own, but the
model they implement existed only as prose spread over `AGENTS.md`, and
several of them re-implemented the same idea:

- `durable_inbox` (`consumer_inbox` table) and the contract command log
  (`contract_command_log` table) are the **same primitive** — "claim a key
  with `INSERT … ON CONFLICT` in the effect's own transaction" — written
  twice, with two prebuilt-statement caches, two purge paths and two
  fallbacks for other dialects.
- The bounds of every replay record (256 KiB of replayed body, 24 h of
  lifetime) were copied into four modules until `sandloom.core.config.replay_limits`
  unified them; command dedupe ran for a while in two namespaces (local and
  RPC) that disagreed; a publish path diverged from the outbox path.
- The post-commit work (outbox dispatch, `on_commit`) was reached from four
  call sites, each with its own ordering.

An external review (GLM, 2026-10) argued that the framework's guarantees are
an accumulation of mechanisms rather than one model. For the dedupe / effect
layer that is right, and this ADR fixes it. It is **not** right for the
security state (revocation and block marks) or for leases: those are not
effects of a business operation and keep their own models (see "Out of
scope").

## Decision

### 1. Vocabulary

**Effect** — a state change a caller asked for and must be able to rely on:
a row written, an event made deliverable, a command executed in another
module, a task handed to the broker, a response the client acts on.

**Boundary** — the place the effect crosses; it decides which guarantee is
even possible:

| Boundary | Meaning |
| --- | --- |
| **in-UoW** | the effect lives in the same database transaction as the business change (one `COMMIT`) |
| **cross-database** | another database of the same process — a second UoW, no shared `COMMIT` (no distributed transaction, ever) |
| **cross-service RPC** | a command contract in another module / process; it runs in the *provider's* own UoW |
| **HTTP client** | the client of an endpoint retries over an unreliable network |
| **event consumer** | an event delivered (at least once) to a consumer that applies it in its own UoW |
| **post-commit** | work that can only start once the UoW committed (broker publish, cache write, notification) |

**Guarantee levels** (strongest first):

| Level | Promise | How it is obtained |
| --- | --- | --- |
| **transactional** | the effect commits iff the business change commits | written in the same transaction |
| **exactly-once-effect** | a retried / redelivered operation applies its effect **on the state of the database holding the record** once (while the record lives) | a **durable dedupe record** claimed in the effect's own transaction (§2) |
| **at-least-once** | the effect eventually happens, possibly more than once | a durable intent (outbox row) plus a retrying delivery |
| **replay-only** | *response deduplication*: a duplicate *request* gets the first response back instead of re-running — while the record lives; the effect itself stays at-least-once | a cache dedupe record (§2, cache form) |
| **at-most-once** | attempted once, never repeated, may be lost (crash, outage); failure is logged / counted, never raised | explicit opt-in (`required=False` + the `# sandloom: best-effort <reason>` marker), or post-commit callbacks |

**Scope of exactly-once-effect** (the end-to-end argument): the durable
record lives in ONE database and commits with the changes of THAT
transaction. Everything else a handler does — a PSP call, an email, a write
to another database, a command contract in another service — is outside the
record and stays **at-least-once** (it may run again when the transaction
that would have recorded it rolls back and is retried). Key such calls on the
operation's own id at the receiving system (the PSP idempotency key, the
command's `idempotency_key`), so the receiver dedupes them.

A mechanism never *upgrades* a guarantee: a replay-only record in front of an
at-least-once effect is still at-least-once for the effect. Money, orders and
provisioning need **exactly-once-effect**, i.e. a durable dedupe record.

**Ordering** is a separate dimension, and no mechanism here provides it: the
outbox, the relay and every consumer form give **no order** across events,
not even within one channel or one aggregate (concurrent UoWs, relay
redeliveries and retries interleave). Consumers order by a per-aggregate
`version` in the payload and apply with version guards (`monotonic_upsert` /
`monotonic_update`, `sandloom.core.projections`). This is a deliberate choice over
keyed / partitioned outboxes: a version guard also survives redelivery and
replay, which a partition order does not.

*Implementation detail, not a guarantee:* the relay does not let a channel's
later rows overtake an earlier row of that channel that is waiting to retry
(head-of-line blocking per channel; other channels are not held). Consumers
must still never rely on order — concurrent UoWs and post-commit dispatch
reorder events anyway. Operational consequence: one poison row delays its
whole channel until it is dead-lettered (`OUTBOX_RELAY_MAX_ATTEMPTS`; watch
`outbox_dead_total` and the outbox age metrics, `sandloom outbox list-dead`).

### 2. The dedupe record — one primitive

Every "this already ran" store in Sandloom is a **dedupe record**:

| Field | Meaning |
| --- | --- |
| **scope** | who dedupes: a consumer group, `<contract>.<method>` + bound tenant (+ `idempotency_scope` arguments), an endpoint scope + caller + tenant, a task namespace + tenant |
| **key** | the operation's identity within the scope: envelope id, idempotency / operation key, `Idempotency-Key` header |
| **args digest** | canonical digest of the operation's inputs; the same key with another digest is a **conflict** (client misuse → `409`/`422`), never a replay |
| **status** | `pending` (claimed, in flight) → `succeeded` (outcome replayable) / `not_replayable` (outcome larger than the cap: never re-run, body not kept) / `processed` (no outcome to replay — an inbox claim) |
| **result** | the encoded outcome kept for replay, at most `REPLAY_BODY_MAX_BYTES` (256 KiB) |
| **TTL** | how long a duplicate is recognised; `REPLAY_RECORD_TTL_SECONDS` (24 h) by default; the consumer inbox has no TTL (purged by age) |

Its operations are the same everywhere: **claim** (atomically create the
record or learn it exists), **complete** (store status + result), **replay**
(return the stored outcome), **conflict** (same key, other digest), **expire
/ purge** (forget it after the TTL).

The shared semantics live in **`sandloom.core.effects`**: the guarantee levels,
the record statuses, the bounds (re-exported from `sandloom.core.config.replay_limits`),
the key digest, the argument digest, the replay-size decision and the
paradigm-agnostic description of a durable record table
(`DedupeRecordSpec`). It has two forms:

- **Durable form** (exactly-once-effect) — `sandloom.core.sql_alchemy.dedupe_log`
  implements claim / complete / replay / purge ONCE for the relational
  paradigm: one prebuilt `INSERT … ON CONFLICT … RETURNING` claim in the
  caller's UoW with autoflush disabled (only *this* record's conflict reads
  as a duplicate), an expired record taken over by `ON CONFLICT DO UPDATE …
  WHERE expires_at <= now` (never a running one — its claim is uncommitted),
  the completion `UPDATE` that restarts the TTL at completion, a `release`
  (`DELETE` of the claim inside the same transaction), a bounded purge that
  re-checks its cutoff on the outer `DELETE`. Three **adapters** map it onto
  their tables (one table per retention rule, never merged: an inbox claim must
  outlive every redelivery of its event, a log record the caller's retry
  horizon):
  - `durable_inbox(consumer)` → `consumer_inbox`, scope = consumer, key =
    envelope id, status `processed`, no digest / result, age-purged;
  - the contract command log (`DatabaseProvider.command_log()`) →
    `contract_command_log`, key = digest of (contract.method, tenant +
    scope arguments, idempotency key), digest + status + result + TTL;
  - the durable HTTP idempotency log (`DatabaseProvider.request_log()`,
    `idempotency_guard(..., durable=True)`) → `http_idempotency_log` (core
    migration `0009_http_idempotency_log`), key = digest of (route scope,
    caller, tenant, `Idempotency-Key`), args digest = the request
    fingerprint, status + stored response + TTL (the guard's `ttl_seconds`,
    24 h). Claimed in the **request's** `DbSession` Unit of Work after a
    transaction-scoped advisory try-lock (an in-flight duplicate gets `409`
    at once, never a wait on the first request's uncommitted claim);
    completed in stage 1 of the commit pipeline (§3) with the response the
    endpoint built (status, headers, body ≤ 256 KiB; larger, streamed or
    anonymous → status only). A returned `5xx` is released, never replayed.
- **Cache form** (replay-only, or fail-closed enqueue dedupe) — the HTTP
  `idempotency_guard` (without `durable=True`), the cache fallback of command contracts, the legacy
  RPC dedupe, the cache consumer inbox and the task-queue enqueue dedupe keep
  their own owner-token / CAS state machines (they differ for good reasons:
  an HTTP request can die without a terminal write, a broker send can be
  ambiguous), but take their TTL, size cap, key tags and (for wire-valued
  inputs: command arguments, task payloads) argument digests from
  `sandloom.core.effects`, so a key, a digest or a bound means the same thing in
  every store. The HTTP guard keeps its request fingerprint (method, path,
  query, caller, tenant, body) as its args digest.

### 3. The commit pipeline — one stage list

Every Unit of Work ends through the same documented pipeline
(`sandloom.core.unit_of_work.COMMIT_PIPELINE`):

1. **before_commit** — the queued `before_commit` checks (skipping those of a
   rolled-back savepoint) and the request's captured commit checks
   (`revocation_at_commit`), each under its registration-time tenant. The
   first failure **rolls the UoW back** and propagates. The durable HTTP
   guard completes its record here (the endpoint's response is already
   built: FastAPI serialises it inside the function-scoped dependency stack
   whose exit commits).
2. **COMMIT** — owned by the `UnitOfWorkContract`; nothing else commits.
   *Failure mode — ambiguous COMMIT* (connection lost while committing): the
   outcome is unknown to the process; stages 3–4 are skipped and the caller
   sees an error, but the transaction may be durable. See §4a.
3. **outbox dispatch** — the committed outbox rows are handed to the bus.
   On the `wait=False` paths (requests, consumers, contract UoWs) this is a
   **hand-off** to the per-loop background coalescer, not a publish: stage 4
   may run before the event reaches the broker, so an `on_commit` callback
   must not assume the event is on the bus. With `wait=True`
   (`db_session()`, Celery, scripts) it publishes in line. A failure is
   logged, counted (`outbox_post_commit_dispatch_failures_total`) and left
   to the relay — it **never fails the committed operation**.
4. **on_commit** — the queued callbacks in registration order, each under
   the tenant bound when it was queued, skipping those of a rolled-back
   savepoint. A failing callback is logged, never raised.

Stage 1 runs inside the `UnitOfWorkContract` (it must precede its
`COMMIT`); stages 3–4 run in ONE function,
`sandloom.core.unit_of_work._dispatch_outbox_after_commit`, called by every commit
path: `open_uow` (request `get_db`, `Database(name)`, `db_session()`,
`consumer_db_session()`), the contract runtime's own UoW, and the test seams
that stand in for a commit (the `get_db` override path and
`sandloom.core.testing.run_on_commit`, which pass `dispatch=False` because the
test owns the transaction and its outbox rows never commit). The contract
runtime's cache-fallback command dedupe records its outcome between stages 2
and 3 (the durable form records it inside the transaction).
`tests/architecture/test_commit_pipeline.py` pins that no other module
reaches the individual stages and that only the UoW contract awaits a
`COMMIT`.

### 4. Mechanism matrix

| Mechanism | Boundary | Guarantee | Record / state | Failure modes |
| --- | --- | --- | --- | --- |
| `publish_event` (in a UoW) + outbox + relay | in-UoW → post-commit → event consumer | **transactional** intent, **at-least-once** delivery | outbox row in the UoW's database | broker down → relay retries with backoff + circuit; a row exhausting `OUTBOX_RELAY_MAX_ATTEMPTS` is dead-lettered (`dead_at`, `outbox_dead_total`, `sandloom outbox requeue`); duplicates possible (post-commit dispatch vs relay race) — the stable envelope id dedupes; no ordering guarantee; with the relay off (`OUTBOX_RELAY_ENABLED=off`, refused in prod unless the `relay_disabled` risk is accepted, §6a) a failed post-commit dispatch is never re-sent: at-most-once |
| `publish_event(required=False)` outside a UoW | post-commit | **at-most-once** | none | lost on a bus error, never repeated; needs `# sandloom: best-effort <reason>` in module code |
| durable consumer (`register_event_consumer`, `durable=True`) + `durable_inbox` | event consumer | **exactly-once-effect** (handler database) | durable dedupe record (`consumer_inbox`) claimed in the handler's UoW | handler error → rollback + redelivery (dead-letter after the bus's limit unless `dead_letter=False`); a claim older than the inbox purge cutoff no longer dedupes a very late redelivery — `sandloom outbox purge --inbox-days` refuses a cutoff inside `EVENT_STREAM_RETENTION_SECONDS` + 1 day (§6a); a `requeue` / `dlq-replay` of an event older than the purge cutoff applies it again; streams trimmed after `EVENT_STREAM_RETENTION_SECONDS` |
| cache consumer inbox (`ConsumerInbox`, `durable=False` consumers) | event consumer | **replay-only** (once per TTL, fail-open) → effect is **at-least-once** | cache record, TTL 24 h | cache outage → no dedupe; mark lost after the effect → re-run |
| contract command dedupe (durable command log) | cross-service RPC / local call | **exactly-once-effect** (provider database) | durable dedupe record (`contract_command_log`) in the provider's UoW | concurrent duplicate waits ≤ `CONTRACT_DB_LOCK_TIMEOUT_MS` → `503 ContractCallInProgress`; same key + other args → `409`; result > 256 KiB → `409 ContractResultNotReplayable` (never re-run); record expires after `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` (the key runs afresh — checked against the relay's redelivery horizon, §6a); NOT atomic with the caller's transaction (saga step) |
| contract command dedupe (cache fallback; backend without `command_idempotency`) | cross-service RPC / local call | **replay-only**, fail-closed | cache dedupe record | crash between commit and the cache write → a retry re-runs; cache outage → `503` |
| legacy RPC dedupe (session-taking methods, deprecated) | cross-service RPC | **replay-only**, fail-closed | cache dedupe record | as above; removed with the legacy methods |
| `idempotency_guard(durable=True)` | HTTP client | **exactly-once-effect** on the request database + replay of the stored response | durable dedupe record (`http_idempotency_log`) claimed in the request's `DbSession` UoW, completed in stage 1 | in flight → `409` at once (advisory try-lock); same key + other request → `422`; rollback / raised error / failing before-commit check → no record (retry runs afresh); returned `5xx` → released; returned `4xx` → replayed (the committed outcome); body > 256 KiB / streamed / anonymous → status-only replay; ambiguous COMMIT → replay or afresh, never twice (§4a); record expires after the guard's TTL (24 h); effects outside the request database stay at-least-once. **Required** on an `operation_class("F")` route that stores responses (startup check, §6a) |
| `idempotency_guard` (cache form) | HTTP client | **replay-only** (in-flight `409`, replay of the stored response, `422` on reuse); `fail_closed=True` refuses on outage | cache dedupe record with owner token; pending TTL 5 min (renewed while the request runs), terminal TTL 24 h | NOT an effect guarantee: a committed effect answered `>= 400` is recorded failed and a retry re-runs; crash or ambiguous COMMIT before the terminal write → re-run; fail-open by default; refused on `F` routes unless `store_response=False` + `fail_closed=True` (in-flight dedupe in front of an endpoint-owned durable record) |
| task-queue enqueue dedupe (`enqueue(..., idempotency_key=)`) | post-commit / broker | **replay-only** for the enqueue (task id replayed), fail-closed; the task effect is **at-least-once** | cache dedupe record; `pending` never expires (ambiguous send), `sent` lives `dedupe_ttl_seconds` | cache outage → `503`; ambiguous publish → `503 task_enqueue_uncertain` until reconciled; broker redelivery not deduped (the task needs its own durable record) |
| `on_commit` | post-commit | **at-most-once**, ordered after the commit | session queue | never runs on rollback, never twice; a failing callback is logged; a process crash or an ambiguous COMMIT loses it — make it retryable or move it behind an event; may run before the committed events reach the broker (stage 3 is a hand-off) |
| `before_commit` | in-UoW (pre-commit) | check, not an effect: failure **aborts** the UoW | session queue | narrows a race to final-read → `COMMIT`; does not close it |
| `revocation_at_commit` | in-UoW (pre-commit), authority in another service | check (serializable, not linearizable — ADR-0003) | before-commit check | revocation store fail-open (dev) accepts during an outage |
| `record_audit` (`required=True`) | in-UoW | **transactional** | audit row in the UoW | outside a UoW → `AuditUnavailableError`; `required=False` is at-most-once and needs the marker |
| `revoke_user_tokens_on_commit` | post-commit (+ pre-commit write with `require_immediate`) | security state, see "Out of scope" | revocation mark + outbox event | — |
| saga runtime (`start_saga`, step protocol — ADR-0008) | in-UoW (start, local steps, transitions) → cross-service RPC (command steps) | start and transitions **transactional** with the owner's change; command steps **exactly-once-effect** at the provider (command log, derived key `saga:<type>:<id>:<step>:do\|undo`); local steps **transactional**; progress **at-least-once** (outbox nudge + lease-guarded timer sweep) | instance row (`saga_instances`, version-guarded state — **not** a dedupe record) + the existing outbox and command log; no consumer inbox (nudges are versioned, the row lock is the guard) | see ADR-0008 §6/§7; a retry or compensation later than `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` after the step's first attempt would run it afresh — refused by the `saga_horizon_beyond_command_dedupe` window and, for operator retries, by `sandloom sagas retry`'s post-TTL rule |

### 4a. Ambiguous COMMIT

The connection can fail while `COMMIT` is in flight (network, failover,
a killed backend): the process cannot tell whether the transaction is
durable. The request answers `5xx`, stages 3–4 never run, and the client
retries. What each mechanism does with that retry:

| Mechanism | If the commit happened | If it did not |
| --- | --- | --- |
| `idempotency_guard(durable=True)` | the record committed with the effect → the retry replays the stored response | no record, no effect → the retry runs afresh |
| `idempotency_guard` (cache form) | the terminal record was never written; the pending reservation is released (5xx) → **the retry re-runs a committed effect** | the retry runs afresh |
| outbox rows (`publish_event`) | durable → the relay delivers them (stage 3 was skipped) | none → nothing to deliver |
| `durable_inbox` / durable consumer | claim + effect committed → the redelivery is a duplicate, skipped | redelivery claims afresh |
| command log (durable) | the caller's retry replays the stored result | runs afresh |
| `on_commit` callbacks | **lost** (at-most-once) | never due |
| task-queue enqueue dedupe | unaffected (the enqueue is not part of the transaction) | — |

The durable records are the only forms that turn an unknown outcome into a
known one; that is why money routes need the durable HTTP guard (or an
endpoint-owned durable record, §6a).

### 5. Out of scope

Not effects of a business operation, so not dedupe records and not governed
by this ADR (they keep their own documented models):

- **Revocation and block marks** (`sandloom:auth:*`) — monotonic security state
  (`set_max`), see `AGENTS.md` and `docs/reference/security-model.md`. Their
  *delivery* reuses the outbox (`core.auth.tokens_revoked.v1`).
- **Leases and ownership** — the beat singleton lease, Streams consumer
  leases and consumer-group ownership fingerprints: concurrency control
  with fencing, not "already ran".
- **One-time security state** — SSO state / 2FA challenge consumption, the
  RPC nonce replay guard (`RPC_REPLAY_GUARD`) — and rate limits: security
  state with its own TTL semantics.

### 6. Rule for new mechanisms (complexity throttle)

A new way to remember "this already ran" or to order work around a commit is
a change to this model, not a local implementation detail:

1. **Reuse first.** An effect needing dedupe uses an existing form: the
   durable dedupe record (`durable_inbox`, a command contract), a `UNIQUE`
   operation-key column on the effect's **own** row, the durable HTTP guard
   (`idempotency_guard(..., durable=True)`), or — for replay only — the
   cache `idempotency_guard` / task enqueue dedupe. Post-commit work uses
   `on_commit` or an event; pre-commit checks use `before_commit`.
2. **A new mechanism needs an ADR amendment**: its row in the matrix (§4)
   — boundary, guarantee level, record, failure modes — and, if it stores a
   dedupe record, an adapter over `sandloom.core.effects` (durable form) or the
   shared helpers (cache form).
3. **Enforced in review, not by a linter.** Whether a claim or a `SET NX`
   is a new dedupe mechanism, a create-or-get, a `UNIQUE` operation key on
   the effect's own row or out-of-scope security state is a judgement a
   pattern rule cannot make reliably; `sandloom check` keeps only deterministic
   rules. (A heuristic `effect-primitives` rule was tried and removed.)

### 6a. Declared requirements and guarantee windows (startup checks)

Requirements are declared next to the mechanism and verified at startup —
deterministic and structural, never a pattern lint:

- **Class-F routes** (`create_app`, `sandloom.core.idempotency.durable.validate_financial_guards`):
  an `operation_class("F")` route whose idempotency guard is the response-
  storing cache form, or fail-open, refuses to start (`FinancialGuardError`).
  Accepted: `durable=True`; or `store_response=False` + `fail_closed=True`
  (in-flight dedupe in front of a durable record the endpoint owns — billing's
  operation intents, whose checkout / portal URL is a bearer credential that
  must not be stored or replayed). An F route without a guard is not refused:
  its dedupe is its own (the PSP webhook by event id, the operator credit
  adjustment by the `operation_key` of its body, a `UNIQUE` ledger key).
- **Guarantee windows** (`sandloom.core.guarantee_windows.guarantee_window_problems`;
  production refuses with `GuaranteeWindowError`, other environments warn;
  each problem carries a stable risk id an accepted-risks list can waive):
  - `command_dedupe_below_retry_horizon` — `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`
    ≥ the relay's redelivery horizon (`OUTBOX_RELAY_MAX_ATTEMPTS` × capped
    backoff, ~18.5 min by default). A saga retrying longer (or across a broker
    outage, or an operator replay) must raise the TTL itself;
  - `inbox_purge_below_redelivery` — `sandloom outbox purge --inbox-days D` refuses
    `D` < `EVENT_STREAM_RETENTION_SECONDS` + 1 day (any `D` when the retention
    is unbounded) unless `--force`;
  - `saga_horizon_beyond_command_dedupe` (ADR-0008 §9) — for every command
    step of every registered saga, `(step deadline, else saga deadline) +
    compensation_deadline` ≤ `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`.

  The third window of the review, `OUTBOX_RELAY_ENABLED=off` in a production
  app process (the outbox loses its retry: at-least-once → at-most-once), is
  refused through the accepted-risks list (`SANDLOOM_ACCEPTED_RISKS`, risk
  `relay_disabled`), whose check knows the gunicorn relay-leader election.

## Consequences

- One relational claim implementation instead of two: a fix to the claim
  (autoflush, lock-timeout mapping, expiry takeover, purge re-check) lands
  in every store at once. The consumer inbox and command log tables are
  unchanged; the adapters keep their public surfaces
  (`DurableConsumerInbox`, `CommandLogStore`, `durable_inbox`). The durable
  HTTP guard adds one additive core table (`http_idempotency_log`) with its
  own retention, purged by the relay housekeeping and copied by
  `sandloom-migrate extract-module` (scope prefix `<module>.`).
- A new dedupe store or post-commit variant is visible at review time
  (analyzer + ADR amendment) instead of being discovered after it drifted.
- The cache forms deliberately stay separate state machines; only their
  vocabulary and bounds are shared. Merging them would weaken one of them
  (the HTTP guard's pending self-healing vs the enqueue's never-expiring
  ambiguous record).
- Cost: the generic claim adds a few Python calls per claim. Cachegrind
  (`CG_K=200`, instr/op; base 742bc604 = mean of 6 runs, branch = 2 runs
  interleaved with 2 of them): HW7 7.568M -> 7.585M (+0.23 %), L6W3 6.410M ->
  6.420M (+0.16 %), HEp 2.798M -> 2.811M (+0.46 %), HEc 4.908M -> 4.907M
  (-0.02 %); the base runs alone spread 0.46 / 0.30 / 0.61 / 1.7 %.
- Cost of the durable HTTP guard (amendment). Cachegrind `CG_K=200`,
  instr/op, base main 88d8aa1a (cache fail-closed guard on `/dw/7`) vs this
  change (durable guard), 2 interleaved runs each: **HW7** (class-F write:
  strict revocation + guard + insert + `publish_event` + `record_audit`)
  7.607M / 7.621M -> 8.153M / 8.137M, mean **+0.53M (+7.0 %)**; control
  **L6W3** (the same write without a guard) 6.430M / 6.420M -> 6.398M / 6.406M, flat.
  The durable form runs three statements inside the request transaction
  (advisory try-lock `SELECT`, claim `INSERT … ON CONFLICT`, completion
  `UPDATE` with the response) where the cache form runs one Redis `SET NX
  GET` before and one CAS after the response. That is the price of a
  record that commits with the effect; non-F routes keep the cheaper cache
  form.

## Related reading

- [Deduplicate effects](../modules/background/effect-deduplication.md)
- [Transaction units](../modules/database/units.md)
