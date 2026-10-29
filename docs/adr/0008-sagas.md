# ADR-0008: Sagas — a small orchestrator over the existing effect primitives

- **Status:** Accepted (2026-10-25; phase 0 and phase 1 built)
- **Date:** 2026-10-25
- **Deciders:** Sandloom maintainers (open questions at the end)
- **Complements:** ADR-0005 (the effect model; this ADR adds one matrix row
  and no new dedupe record), ADR-0007 (kernel layers; the placement below),
  ADR-0003 (billing operation intents, the hand-rolled saga this generalises
  later, not now)
- **Research:** `docs/design/sagas-research-2026-10-25.md` (internal research, retained in the original private repository)
  (inventory, survey with sources, option analysis, worked example)

## Context

Cross-module writes in Sandloom are sagas by construction. A command contract runs
in the provider's own Unit of Work and is "NOT atomic with the caller's
transaction — design it as a saga step" (`AGENTS.md`). There is no distributed
transaction, and there never will be (ADR-0005 §1). The framework already has
every primitive a saga step needs:

- **the outbox and its relay:** a durable intent written in the same
  transaction as the business change;
- **durable consumers and `durable_inbox`:** exactly-once effect per event;
- **command contracts with the durable command log:** exactly-once effect at
  the provider, keyed by an `idempotency_key`, local and RPC alike. A
  concurrent repeat waits for the in-flight call and never runs twice;
- **the task queue, the beat lease and projections.**

What it lacks is the part that turns those primitives into a business process:

- a place where "where is this process now?" is recorded;
- a rule that derives step operation keys;
- forward and backward recovery;
- deadlines;
- a way for an operator to list, retry, compensate or park a stuck instance.

Today each flow writes this by hand. Billing's operation intents (ADR-0003 G1)
and their reconciler are a complete hand-rolled saga, about 1,330 lines of
service code plus tests. `docs/ROADMAP.md` lists "Saga / process-manager" as a capability gap,
and review C2 #13 proposes `sandloom.core.sagas`. Without a shared shape, the next
flows will each invent their own:

- org onboarding;
- user erasure (review C5 N5);
- org deletion → subscription cancel.

## Decision drivers

1. **Reuse ADR-0005.** ADR-0005 forbids a new dedupe store or post-commit
   variant unless an amendment justifies one.
2. **Local equals remote.** Monolith and split deployments must produce the
   same outcome.
3. **Only deterministic checks.** Every check is structural and runs at
   startup or registration. None is a heuristic lint.
4. **Module isolation.** A saga talks to other modules only through command
   contracts and events, referenced by string.
5. **Fits ADR-0007.** The saga runtime has a place in the layer map, and it
   adds no `ignore_imports`.
6. **Pragmatic.** Reuse the existing infrastructure and keep orchestration off
   the request hot path. This decision preceded the initial public alpha.

## Considered options

| | (A) Choreography + helpers | (B) Kernel orchestrator over existing primitives | (C) External durable-execution engine as a `workflow` driver |
| --- | --- | --- | --- |
| Where state lives | spread over participants' rows | one instance row in the owning module's database | the engine's store (Temporal cluster, Restate journal, DBOS system tables) |
| Start atomic with the business change | yes (outbox event) | yes (instance row + outbox row in the same UoW) | no: dual write unless started from an outbox consumer |
| Step effect | exactly-once at the provider (command log) | the same | the same: activities still call Sandloom commands with a key |
| Ops ("where is it?", retry, compensate) | none: grep logs and tables | `sandloom sagas list/show/retry/compensate/park`, metrics, audit | engine UI (good), but outside Sandloom's audit, tenancy and RLS |
| New infrastructure | none | none: two core-chain tables | a cluster or server, plus a new store, a new SDK, and determinism rules |
| ADR-0005 fit | full | full: no new dedupe record (see Decision §9) | new dedupe and step store, which needs an amendment |
| ADR-0007 fit | trivial | new `sagas` package in the contracts & RPC layer | a driver under `drivers` and a plugin |
| Effort | S | M (MVP), L (full) | M per engine, plus ops for the engine |

(A) stays the right tool for fan-out reactions without compensation (erasure,
notifications). (C) stays possible as a plugin through the existing
experimental `workflow` capability contract. Neither gives the default
platform a saga with central state, deadlines and operator control at zero
infrastructure cost.

## Decision

Adopt **(B)**: `sandloom.core.sagas` is a small orchestrator. A saga is a named,
versioned, **linear** sequence of steps, with backward recovery (compensation)
before its pivot and forward recovery (retry) after it. All the effect
guarantees come from primitives that already exist. The orchestrator only adds
the instance state machine, which is version-guarded state, not a dedupe
record. Phase 0 also ships the (A) guidance. (C) is documented as a plugin
path.

### 1. Definition (registration-time checks)

A module declares sagas in `setup` with `core.register_saga(definition)`. A
definition has:

- a name, unique per module (`<module>.<name>`);
- a `version`;
- an input model, which is persisted in the owner's database and never crosses
  RPC, so it may live in the module;
- an overall `deadline`;
- an ordered list of steps.

Each step is one of the following:

| Step form | Effect boundary | Guarantee |
| --- | --- | --- |
| `step(call="svc.vN.method", args=..., compensate="svc.vN.method", compensate_args=...)` | cross-service command contract | exactly-once-effect at the provider (durable command log), under a derived key (§3) |
| `local_step(apply=fn, compensate=fn)` | in-UoW of the owner | **transactional** with the instance transition |
| `await_event(channel, correlate=..., deadline=...)` *(phase 2)* | event consumer | the reply is applied in the transition UoW; deadline racing it is serialized by the row lock |

A step also has a `kind`. The three kinds follow Garcia-Molina & Salem and
Richardson:

- `compensatable`: may be undone;
- `pivot`: the go/no-go point, at most one;
- `retriable`: after the pivot, must eventually succeed.

`register_saga` refuses the following, deterministically:

- duplicate step names;
- a compensatable step without a compensation;
- a compensation on a pivot or retriable step;
- a step before the pivot that is not compensatable, or a step after it that
  is not retriable;
- more than one pivot;
- a `call=` that is not a command contract of a registered local provider.

For a remote provider, the bound contract's describe document is checked by
the existing contract-compat check. Registering a saga adds a
`ContractRequirement(name, methods=(...))` for every step to the module's
`consumes`. So a missing provider is a `ContractBindingError` at startup, as
for any other consumed contract.

### 2. State: two core-chain tables in the owning module's database

`saga_instances` and `saga_steps` join the core chain. That chain already
holds the shared infra tables that every hosting database receives (outbox,
audit, inbox, command log). The rows live in the database that hosts the
owning module, the same rule as its outbox. `saga_type` is prefixed
`<module>.`, so `sandloom-migrate extract-module` copies a module's instances with
it, the way it copies command-log records.

- **`saga_instances`** has these columns: `id`, `saga_type`,
  `definition_version`, `saga_id` (the business id, `UNIQUE (saga_type,
  saga_id)`), `tenant_id`, `status`, `step_index`, `direction` (`forward` /
  `backward`), `attempt`, `next_attempt_at`, `deadline_at`, `input`
  (JSON), `results` (JSON, step name → result), `last_error`, `version`,
  `created_at`, `updated_at`. The status is one of `running`, `compensating`,
  `completed`, `compensated`, `failed` or `parked`.
- **`saga_steps`** holds one row per `(instance, step, direction)`, updated in
  place: `status`, `attempts`, `first_attempt_at`, `last_attempt_at`,
  `last_error`. It exists for `sandloom sagas show` and for metrics. It is not a
  dedupe record.

These tables are not `TenantScoped`. Like the outbox, they are kernel infra.
The runtime binds the instance's `tenant_id` while it runs a step, so command
calls carry the signed tenant context. `input` and `results` must hold ids,
not PII; the same rule applies to event payloads.

**Row-level security.** Under `tenant_rls` (and therefore under
`PRODUCTION_PROFILE=multi_tenant_strict`) both tables are migrated with
`enable_tenant_rls(op, "<table>", system_access=True)`. The runtime always
touches an instance with its `tenant_id` bound, so the app role sees exactly
the rows of the instance it drives. Cross-tenant reads and writes — the timer
sweep, `sandloom sagas list` without `--tenant`, and every operator mutation — go
through the kernel's audited system channel (`system_session(actor=,
reason=)`), never through `unscoped()`. The strict profile's startup check
covers both tables like any other tenant table.

### 3. Operation keys

The runtime injects every step's idempotency argument, using the contract's
declared `idempotency_key` parameter:

```text
saga:<saga_type>:<saga_id>:<step>:do      # forward
saga:<saga_type>:<saga_id>:<step>:undo    # compensation
```

Arguments are recomputed on every attempt from the persisted `input` and
`results`. The command log's argument digest therefore catches a
non-deterministic `args` callable: the same key with other arguments returns
`409 ContractIdempotencyConflict`. The runtime parks the instance with
`nondeterministic_args`. No new mechanism is needed, because the existing
record already enforces it.

### 4. Start: transactional with the business change

`await start_saga(ORG_SETUP, saga_id=org.id, input=...)`, imported from
`sandloom.core.di`, runs inside the caller's UoW. It inserts the instance row and
publishes `<module>.saga_advance.v1` through the outbox in the same
transaction. Starting the same `saga_id` again is a create-or-get on the
`UNIQUE` key. If the input differs, the call is a conflict (`409`); if it
matches, it returns the existing instance. Outside a UoW, `start_saga` raises
`UnitOfWorkRequiredError`, like `publish_event`.

### 5. Driving: an outbox-fed consumer plus a timer sweep

- **Advance:** each module that registers a saga gets a kernel-installed
  consumer `<module>:sagas@advance` on its private channel
  `<module>.saga_advance.v1`. It runs with `dead_letter=False`, because a saga
  must never be silently parked in a DLQ, and on the owning database. In a
  split deployment, only the owning module's service consumes it.

  An advance event is a **nudge, not a command**: its payload is
  `{instance_id, expected_version}`. The handler locks the row and does
  nothing unless the row is active, due, and still at `expected_version`. A
  duplicate, a late or a reordered nudge is therefore a no-op by the state
  check alone. The consumer is registered **without** the durable inbox
  (`durable=False`, an existing consumer option, not a new mode): an inbox
  claim would protect nothing the row lock and version do not already
  protect, and would add an `INSERT` per step and a growing table.
- **Timer sweep:** a kernel loop in every process that hosts a saga-owning
  module, guarded by a fenced cache lease (the `beat_lease` mechanism, one
  holder per database). It is **not** tied to the outbox relay: the relay is
  off by default outside prod, and dev must drive sagas exactly as prod does.
  Each tick selects active instances whose `next_attempt_at` or `deadline_at`
  is due, `FOR UPDATE SKIP LOCKED LIMIT n`, and publishes a nudge for each
  one. It also stamps `swept_at` so it re-drives at most once per interval
  (`SAGA_SWEEP_INTERVAL_SECONDS`, an ops knob, default 5 s).

  Durable timers are therefore a column on the instance row, with no new
  store. Celery `countdown`/ETA is rejected for timers, because those messages
  sit in worker memory and depend on broker visibility timeouts.

### 6. The step protocol (one UoW per step)

```text
advance(nudge):                                     # consumer UoW, owner DB
  row ← SELECT … FOR UPDATE SKIP LOCKED             # none → another worker owns it; ack
  if row terminal or row.version ≠ nudge.expected_version
     or row.next_attempt_at > now: ack              # duplicate / stale / early: no-op
  if row.deadline_at ≤ now: → timeout transition (§7), commit
  bind_tenant(row.tenant_id)
  outcome ← run current step (command call with its key | local fn in this UoW)
  apply transition (§7): results, step_index ± 1, attempt, next_attempt_at,
                         status, version + 1; upsert saga_steps
  publish <module>.saga_advance.v1 (if more to do)   # outbox, same transaction
  commit                                             # pipeline of ADR-0005 §3
```

The instance row lock is held across one step call. That call is bounded by
`SERVICE_RPC_TIMEOUT_SECONDS` (1 s default) plus one transport retry, or by
the local contract pool's `lock_timeout`. A command runs on the contract pool
or in another process, never on this connection. The provider never touches
saga tables, so the lock cannot deadlock with the step. Holding the lock
serializes everything that can move an instance: concurrent deliveries, the
timer, an arriving reply event, and operator commands.

**Why one transaction per step, not two.** A "lease, commit, call, commit"
variant would release the connection during the call, but it needs a lease
with an expiry on the row, a reclaim rule for expired leases, and it reopens
exactly the races the lock closes (timer vs reply vs operator). The hold is
bounded (≈ 1–2 s worst case, one connection per in-flight step, steps run off
the request path), so the MVP keeps the single transaction and measures it:
the benchmarks suite gets a saga rung, and `docs/operations/operator-reference.md` gets sizing
guidance (consumer concurrency × one connection). The two-transaction variant
is reconsidered only if that measurement shows pool pressure.

How each failure is handled (each maps onto ADR-0005 §4/§4a):

| Failure | What happens |
| --- | --- |
| crash before the call | the transaction rolls back; the sweep re-drives; the step runs |
| crash after the provider committed, before our commit | the re-drive re-calls with the **same key**; the durable command log replays the result; the transition commits |
| ambiguous COMMIT of the transition | committed: the outbox advance row exists, and the next step runs. Not committed: as above |
| duplicate advance delivery | the row lock and the state check make it a no-op |
| provider in flight when we re-call | `503 ContractCallInProgress`; transient; retried with backoff. The command log never runs it twice |

### 7. Recovery rules

**Classifying a step outcome.** The class is decided by an explicit,
closed table in `sandloom.core.sagas.outcomes`, never by the first digit of a
status code. An unknown status is **transient** (retrying is the safe default:
the command log makes a repeat harmless, and the deadline bounds it).

| Class | What maps to it |
| --- | --- |
| `success` | the contract returned |
| `transient` | `408`, `423`, `425`, `429`, every `5xx` (incl. `501 ServiceUnavailableInDeployment` while a binding rolls out, `503 RemoteServiceUnavailable`, `503 ContractCallInProgress`), transport timeouts and connection errors, a local `lock_timeout`, any status not listed below |
| `refused` (definitive business refusal) | `400`, `402`, `403`, `404`, `410`, `422` raised by the provider's domain code (`ContractCallError` with the provider's status) |
| `conflict` | `409 ContractIdempotencyConflict` (same key, other arguments → non-deterministic `args`), `409 ContractResultNotReplayable` |
| `misconfigured` | `401` and `ContractArgumentError` (`422` raised by the binding layer, not by the provider's domain code) — a wiring bug, not a business answer |

A module may narrow this per step (`step(..., refuse_on={409})` for a
provider whose domain conflict is a real refusal); it can never widen
`transient` to `refused` for `408/425/429/5xx`.

| Outcome of a step call | Forward, before or at the pivot | Forward, after the pivot | Backward (compensating) |
| --- | --- | --- | --- |
| success | next step, or `completed` | next step, or `completed` | previous step, or `compensated` |
| refused | **start compensation** from the last applied step | **park** (`retriable_refused`); a retriable step must not refuse | **park** (`compensation_refused`) |
| transient | retry with exponential backoff and full jitter, until the step or saga deadline | retry with capped backoff **until the deadline, then park** | retry with capped backoff until `compensation_deadline`, then **park** |
| conflict | park (`nondeterministic_args`) | park | park |
| misconfigured | park (`misconfigured`) | park | park |
| saga `deadline_at` passes | start compensation | park | park |

**Resolve before compensating.** A step whose last attempt was ambiguous
(transient error or timeout) is not compensated blindly. The runtime first
re-issues the forward call with the same key. The durable command log either
replays the committed outcome, makes the call wait for the in-flight one, or
runs the step now. The step is then known to be applied, or definitively
refused, and only an applied step is compensated.

This closes the "compensation overtakes a late forward" hole. Seata calls it
the empty-rollback / suspension problem of TCC. Sandloom closes it with the
existing command log, without a provider-side tombstone. Compensations must
still be idempotent and must accept "nothing to undo".

**Timeout racing completion.** Deadlines, replies and step outcomes all move
the instance under the same row lock with a version bump. Whichever commits
first wins, and the loser sees the new state and does nothing. A reply that
arrives after a timeout has started compensation is recorded in `saga_steps`.
The awaited step is then treated as applied, so its compensation runs. This is
the phase-2 `await_event` case.

**Parking** sets `status = parked` with a reason. It increments
`sandloom_saga_parked_total{saga_type, reason}` (alert on it) and writes an audit
row (`core.saga.parked`, `required=True`). A parked instance never moves
without an operator. "Retry forever" is not the default for business
compensations, because an operator must see a compensation that cannot
succeed. Kernel security effects stay on `dead_letter=False` consumers, as
today.

### 8. Operations

| Command | Effect | Audit action |
| --- | --- | --- |
| `sandloom sagas list [--type T] [--status S] [--older-than D] [--tenant ORG]` | list instances | — |
| `sandloom sagas show <saga_type> <saga_id>` | show the instance and its step table | — |
| `sandloom sagas retry …` | clear `next_attempt_at`, un-park to the parked direction, publish an advance — **refused** while the current step's dedupe record may have expired (see below) | `core.saga.retried` |
| `sandloom sagas compensate …` | operator abort; allowed only before the pivot | `core.saga.compensation_forced` |
| `sandloom sagas park …` | stop an instance | `core.saga.parked` |
| `sandloom sagas resolve … --step S --as applied\|skipped --reason R` | an operator asserts a step's outcome, as in Camunda incidents | `core.saga.step_resolved` |

The mutating commands are dry runs unless given `--apply --reason`. In prod
they also need `--confirm-production`, the same convention as
`sandloom security …`. Every mutation is audited (`required=True`).

**Retry after the dedupe window.** The window check (§9) bounds automatic
retries, but an operator can retry a parked instance days later. By then the
command-log record of the current step may have expired, and a blind re-call
would run the step a second time. So `saga_steps.first_attempt_at` is
compared with `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` (minus a safety margin of
one sweep interval plus the RPC timeout):

- **inside the window:** `retry` re-drives as usual; the command log replays
  or waits for whatever already happened;
- **outside the window, and the step's outcome is unknown** (last attempt
  transient or ambiguous): `retry` refuses with exit code 1 and a hint. The
  operator must first establish the outcome with the provider (its own
  records, `sandloom sagas show` prints the step's operation key to search for)
  and record it with `sandloom sagas resolve --step S --as applied|skipped`.
  `resolve` therefore ships in the **MVP**, not in phase 2. After that the
  instance continues from a known state;
- **outside the window, but the outcome is known** (the last attempt was a
  definitive refusal, or the step was never attempted): `retry` proceeds — a
  re-call cannot double an effect that never happened.

The same rule applies to an instance un-parked from `unknown_definition`
after a deploy: the runtime refuses to re-call a step whose record may have
expired and parks it with `outcome_unknown_after_ttl` instead.

Metrics:

- `sandloom_saga_instances{saga_type,status}`, a gauge from a bounded query on the
  sweep tick;
- `sandloom_saga_step_duration_seconds`;
- `sandloom_saga_step_attempts_total{outcome}`;
- `sandloom_saga_parked_total`.

`/diagnostics/core` lists the registered definitions and versions. `sandloom
doctor` warns about parked instances and instances older than their deadline.
An owner-console page comes later.

### 9. Effect-model amendment (ADR-0005 §4, one new row; no new record)

| Mechanism | Boundary | Guarantee | Record / state | Failure modes |
| --- | --- | --- | --- | --- |
| saga runtime (`start_saga`, step protocol) | in-UoW (start, local steps, transitions) → cross-service RPC (command steps) | start and transitions **transactional** with the owner's change. Command steps **exactly-once-effect** at the provider (command log, derived key). Local steps **transactional**. Progress **at-least-once** (outbox advance plus timer sweep) | instance row (version-guarded state, **not** a dedupe record), plus the existing outbox and command log (no consumer inbox: advances are versioned nudges, §5) | see §6/§7. A retry or compensation later than `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` after the first attempt would run the step afresh, which is refused by the window check below |

The instance row answers "where is the process". It does not answer "did this
effect already run" for any effect outside its own transaction. That question
is always answered by the command log. Local steps are protected by the
transition commit itself, which is ADR-0005 §6.1's "state on the effect's own
row". The saga runtime therefore needs no new dedupe store.

**New guarantee window** (`sandloom.core.guarantee_windows`, deterministic):
`saga_horizon_beyond_command_dedupe`. For every command step of every
registered definition, the longest span from the step's first attempt to its
last possible retry or resolve must fit the dedupe TTL:

```text
(step deadline, else saga deadline) + compensation_deadline
    ≤ CONTRACT_COMMAND_DEDUPE_TTL_SECONDS
```

Otherwise a late retry or resolve could re-execute a step whose record
expired. Waiting in an `await_event` step holds no command key open, so it
does not count. Production refuses this with `GuaranteeWindowError`; other
environments warn. It can be waived through `SANDLOOM_ACCEPTED_RISKS` (ADR-0006),
but only as a recorded risk.

### 10. Placement in the kernel layers (ADR-0007)

| Part | Package | Layer | Imports (all downward or sibling, acyclic) |
| --- | --- | --- | --- |
| DSL: `Saga`, `step`, `local_step`, `SagaContext`, `StepKind` (pure dataclasses) | `sandloom.core.descriptors` (re-exported by `sandloom.core.module_api`) | foundation | — |
| instance store: statements, sweep query, models | `sandloom.core.sql_alchemy.saga_store`, `sql_alchemy/models/saga.py`, a core-chain migration | persistence | `effects`, `unit_of_work` |
| timer sweep loop (lease-guarded, §5) | `sandloom.core.sagas.sweep`, started by composition | contracts & RPC | `sql_alchemy.saga_store`, the beat-lease primitive, `consumers` (publish via outbox) |
| runtime: step protocol, consumer install, recovery rules | **`sandloom.core.sagas`** (new) | contracts & RPC (sibling of `module_api`) | `module_api` (service calls), `consumers`, `sql_alchemy.saga_store`, `unit_of_work`, `descriptors` |
| `register_saga` | `sandloom.core.module_api` | contracts & RPC | stores the descriptor on the facade registry (duck-typed, like `register_event_consumer`), so `module_api` never imports `sagas` |
| `start_saga` | `sandloom.core.di` → `sandloom.core.sagas` | composition → contracts | downward |
| wiring (install consumers and sweep loop at startup) | `app_factory` / `lifecycle` | composition | downward |
| CLI `sandloom sagas` | `sandloom.core.cli.sagas` | tooling | everything |
| test seams | `sandloom.core.testing.sagas` | tooling | everything |

`sagas` is added to the `kernel-layers` contract next to `module_api`. Because
`module_api` must not import `sagas`, the acyclic-siblings contract holds.

### 11. Versioning in-flight instances

An instance is pinned to `definition_version`. A new version is a new
definition registered **alongside** the old one, for as long as old instances
run. `sandloom sagas list --type T --version V` shows them, and a later release
removes the old one. If the advance finds an instance whose `(type, version)`
is not registered, it parks the instance with `unknown_definition`. The
consumer never crashes. `sandloom doctor` and the startup task report such
instances.

Editing a registered version in place is not detected at runtime. Instead,
step names, kinds and called contracts are pinned per version in the module's
committed `contracts.snapshot.json`, under a `sagas` key. A stale snapshot
fails the suite, the same mechanism as contract snapshots, and it is
deterministic.

### 12. What sagas do not provide

- **Isolation (ACD, no I).** Intermediate states are visible. Modules apply
  Richardson's countermeasures explicitly. The most common one is a
  **semantic lock**, a status column such as `provisioning` that routes refuse
  until the saga completes. The others are commutative updates, reread value
  (version re-check, ADR-0003 G1), by value and version file. The framework
  documents them and does not automate them.
- Parallel steps, nested or child sagas, branching, loops, human tasks. A
  definition is linear. Branching belongs in a step's arguments or in two
  sagas.
- Ordering across sagas, and any atomicity with external systems. A PSP call
  is a local step that keys its own idempotency on the derived key, as billing
  intents do today.

### 13. Testing

The `saga_harness` fixture (§14) drives the real protocol synchronously, with
fault injection (`fail_step`, `crash_after`, `deliver_twice`), an injectable
clock and the `assert_compensations_idempotent` /
`assert_resolves_before_compensating` checks. The kernel's own suite adds a
fault test for every row of the §6 and §7 tables and for the post-TTL retry
rule (§8).

A split-conformance script case runs the worked example in the monolith and in
split deployments, and requires the same terminal state.

### 14. Developer experience

A saga is only worth having if writing, testing and debugging one is easier
than hand-rolling it. The DX rules below are part of the decision, not polish.

**Authoring — typed, declarative, named functions.**

```python
# sandloom/<module>/sagas.py — the one place a module's sagas live
from datetime import timedelta

from sandloom.core.module_api import Saga, SagaContext, local_step, step

from .schemas.sagas import EnrollInput, Enrollment


def charge_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {"customer_id": c.input.customer_id, "amount": c.input.price}


def refund_args(c: SagaContext[EnrollInput]) -> dict[str, object]:
    return {"customer_id": c.input.customer_id, "amount": c.input.price,
            "of_operation_key": c.key("charge")}


ENROLL = Saga[EnrollInput](
    "enroll", version=1, input=EnrollInput,
    deadline=timedelta(minutes=30),
    steps=[
        step("charge", call="billing.credits.v1.spend", args=charge_args,
             compensate="billing.credits.v1.reverse", compensate_args=refund_args),
        local_step("grant_access", kind="pivot", apply=Enrollment.activate),
    ],
)
```

- `Saga[InputT]` and `SagaContext[InputT]` are generic, so `c.input` is the
  module's own pydantic model with IDE completion and mypy checking.
  `c.result("charge", SpendResult)` returns a step's decoded result typed.
- Arguments are **named module-level functions**, not lambdas: they show up
  in tracebacks and coverage, can be unit-tested, and the docs and the
  scaffold use them. (Lambdas work; the guide recommends against them.)
- Keyword-only builders with defaults: `kind` defaults to `compensatable`
  before the pivot and `retriable` after it, so a typical step names only
  `call`, `args` and its compensation.
- `call=` stays a string (`"<contract>.<method>"`), like every cross-module
  reference in Sandloom — modules never import each other.

**Fail at startup with a fix, never at step 3 in production.** Every
registration error names the saga, the step and the remedy, in the same
`fix:` style as capability errors:

```text
SagaDefinitionError: tenancy.org_setup step "assign_owner_role":
  billing.credits.v1.grant has no parameter "tennant_id" (did you mean "tenant_id"?)
  fix: return the provider's parameter names from grant_args()
```

Checked deterministically at registration: the structure rules of §1; the
called method exists and is a **command** (`command=True`, with an
`idempotency_key`); and the parameter **names** the `args` function returns
match the method's signature. The names are read from the provider's describe
document — the local provider at registration, a remote one from the module's
committed contract snapshot (and again at `/ready` by the existing compat
check). The check calls each `args` function once with a synthetic
`SagaContext` built from the input model's schema; a function that cannot run
on synthetic input opts out per step (`check_args=False`) and is then covered
only by tests.

**Starting from an endpoint — one recommended HTTP shape.** A saga is
asynchronous, so the route that starts one answers `202 Accepted` with the
instance's status URL; `saga_status()` gives the read side:

```python
@router.post("/enrollments", status_code=202,
             dependencies=[Depends(operation_class("W"))])
async def enroll(body: EnrollIn, db: DbSession, user=Depends(get_current_user_id),
                 _=Depends(idempotency_guard(scope="enroll", durable=True))):
    handle = await start_saga(ENROLL, saga_id=body.enrollment_id, input=...)
    return {"status": handle.status, "status_url": f"/enrollments/{handle.saga_id}"}
```

`start_saga` returns a `SagaHandle(saga_id, status, created)`; a repeat with
the same input returns the existing handle (`created=False`), so the route
needs no special idempotency code of its own beyond the guard.

**Scaffold.** `uv run sandloom add saga <module> <name>` generates, in the
module's own package:

- `sagas.py` with a two-step definition and its argument functions;
- `schemas/sagas.py` with the input model;
- the `register_saga` line in `setup`;
- `tests/test_saga_<name>.py` with the happy path, a refusal before the
  pivot, a crash after the provider committed, a timeout that resolves before
  compensating, and `assert_compensations_idempotent` — all green on the
  generated code;
- a commented `202` route.

`sandloom new` apps get the same command. The generated code passes `sandloom check`
and the operation-class test.

**Testing — synchronous, deterministic, no sleeps.** The `saga_harness`
fixture (from the `sandloom.core.testing` plugin) drives the real protocol in the
test's process:

```python
async def test_refund_when_access_cannot_be_granted(saga_harness):
    saga_harness.fail_step("grant_access", error="refused")
    run = await saga_harness.run(ENROLL, saga_id=..., input=...)
    assert run.status == "compensated"
    assert run.calls == ["charge:do", "grant_access:do", "charge:undo"]
    run.assert_keys_derived()            # every call carried its derived key
```

- `run()` executes the steps synchronously through the real advance path
  (row lock, transition, nudges) and returns a `SagaRun` with the status, the
  ordered calls, the keys, the persisted results and the step table;
- `fail_step(name, times=n, error="transient"|"refused"|"timeout")`,
  `crash_after(step)` (the provider committed, our transition did not),
  `deliver_twice()`, and `clock.advance(timedelta)` for deadlines and backoff;
- `assert_compensations_idempotent(definition)` and
  `assert_resolves_before_compensating(definition)` are one-liners a
  module's suite is expected to contain (the scaffold writes them);
- providers are the real ones when co-loaded, or fakes registered with
  `saga_harness.fake_contract("billing.credits.v1", spend=..., reverse=...)`
  that still go through the command-log dedupe, so key behaviour is real.

**Seeing what a saga did.** `sandloom sagas show <type> <id>` prints a timeline,
not a table dump:

```text
tenancy.org_setup  4f1c…  status=parked (compensation_transient)  v1  tenant=9a2e…
  started 10:02:11 by POST /orgs  trace=7b3d…
  ✓ grant_trial_credits  do    10:02:11  1 attempt   42 ms  key saga:…:grant_trial_credits:do
  ✗ assign_owner_role    do    10:02:12  refused 404 user_inactive
  ↺ grant_trial_credits  undo  10:02:12 → 11:02:12  14 attempts  last: 503 billing unavailable
  why parked: the compensation kept failing past compensation_deadline (1h)
  next:       fix billing, then `sandloom sagas retry tenancy.org_setup 4f1c… --apply --reason …`
```

- every park reason has a fixed, documented remediation that `show` prints;
- each step runs in its own trace span, linked to the trace that started the
  saga (the start's `trace_id` is stored on the instance), and every log line
  of a step carries `saga_type`, `saga_id`, `step`, `direction` and `attempt`;
- `sandloom sagas list` defaults to the actionable view (parked, overdue,
  compensating) with ages; `--all` shows everything;
- phase 2 adds the same timeline as an owner-console page.

**Local dev behaves like prod.** The timer sweep runs in every process
hosting a saga module (§5), the advance consumer runs on the dev bus, and no
"inline mode" exists that would skip the real path. `make_headers` +
`client` + `saga_harness` are enough for any test.

**Docs and agent rules.** `docs/concepts/sagas.md` opens with a decision
tree (one module → plain UoW; fan-out without undo → choreography; several
modules with undo → saga), then the countermeasure catalogue and recipes.
`AGENTS.md` gets a short block of the rules an author (human or AI) must
follow: sagas live in `sagas.py`; `args` are pure named functions of `input`
and `results`; every compensatable step names a compensation command that
accepts "nothing to undo"; retriable steps never refuse; a semantic lock
guards user-visible intermediate states; a definition change is a new
`version`.

## Consequences

- Cross-module business processes get one shape:
  - central state;
  - derived keys;
  - deadlines;
  - parking;
  - an operator CLI.

  There are no new infrastructure, dedupe stores or post-commit variants.
- There is no cost on requests that do not start a saga. Starting one adds
  an instance `INSERT` and an outbox row to the caller's flush. Each step costs
  4 statements in its own UoW (lock, upsert step, update instance, outbox
  insert — no inbox claim, §5), plus the step's command, which already pays
  its command-log claim and update. The step also pays one Streams hop.
- The runtime holds a pooled connection and an instance row lock for one step
  call, about 1–2 s worst case (§6 explains why that is kept). Sizing guidance
  goes into `docs/operations/operator-reference.md`; a saga rung joins the benchmarks suite.
- Module authors must write compensations that are idempotent and accept
  "nothing to undo". They must also make retriable steps unable to refuse.
  The structure checks and the test seam make the gap visible, not
  impossible.
- `register_saga` adds consumed-contract requirements, so a module owning a
  saga does not boot without its providers, locally or through bindings. This
  is the intended, explicit failure (`test_module_boots_alone` pins it).

## Phased plan

| Phase | Scope | Size |
| --- | --- | --- |
| **0: guidance** | `docs/concepts/sagas.md`: when choreography (A) suffices; the key-derivation rule; the countermeasure catalogue; "compensation is a command contract too". `sandloom.core.di.operation_key(*parts)` helper | S |
| **1: MVP** | linear definitions with `step` and `local_step` (typed, §14); registration checks incl. argument names; core-chain tables with RLS (§2); `start_saga` / `saga_status`; nudge consumer without inbox; lease-guarded timer sweep; outcome classification and recovery rules §7 incl. resolve-before-compensate; parking; window check; the post-TTL retry rule; `sandloom sagas list/show/retry/park/resolve`; timeline output; metrics and trace linking; `saga_harness`; `sandloom add saga`; concept page and `AGENTS.md` block; a saga benchmark rung; the worked example **in an example app**, not in the bundled modules (below); split-conformance case | M |
| **2: process manager** | `await_event` steps (correlation key column, `UNIQUE (saga_type, correlation_key)`); `sandloom sagas compensate`; per-version snapshot; owner-console timeline page | M |
| **3: migrations** | org deletion → subscription cancel; evaluate moving billing's intent **reconciler** (not the request path) onto a saga with local PSP steps | M |
| later, on demand | (C) plugin driver for the `workflow` capability (Temporal or DBOS), started from an outbox consumer | M per engine |

## Amendments (implementation of phases 0 and 1, 2026-10-25)

The phase-1 build follows the decision above. Where it had to differ, the
reason is recorded here; this section supersedes the text it names.

- **A1 — statuses (§2).** An instance is `running`, `compensating`,
  `completed`, `compensated` or `parked`. `failed` is not produced: every
  outcome that is neither a success nor a finished compensation parks, so an
  operator sees it (§7). A compensated saga is the business "failure".
- **A2 — instance columns (§2).** Besides the listed columns the row carries
  `resolving` (the current step must be re-issued before compensating),
  `last_outcome` and `step_first_attempt_at` (the post-TTL guard reads them
  without an extra `saga_steps` read — the transition stays at four
  statements), `step_deadline_at` / `compensation_deadline_at`, `applied`
  (the forward steps whose effect exists; compensation walks only those, so
  a step resolved as `skipped` is never undone), `park_reason` /
  `parked_from` (the direction `retry` returns to), `trace_id` /
  `started_by` (§14) and `swept_at` (§5). `saga_steps` also carries
  `saga_type` and `tenant_id` (extraction filter, RLS), the step's
  `operation_key`, `outcome` and `last_duration_ms`.
- **A3 — the nudge carries the tenant (§5).** `{instance_id,
  expected_version, tenant_id}`: under `tenant_rls` the consumer must bind
  the instance's tenant BEFORE the row lock, or RLS hides the row. The value
  authorizes nothing — a wrong tenant only makes the row invisible (a no-op).
- **A4 — deadline before "early" (§6).** The advance checks the deadline
  first, then `next_attempt_at > now`: a deadline must fire while a retry
  is backing off. A transient failure publishes no nudge; the sweep re-drives
  the instance when its backoff (or a deadline) is due, so retry latency is
  `max(backoff, SAGA_SWEEP_INTERVAL_SECONDS)`.
- **A5 — the sweep's transactions (§5).** The claim (`FOR UPDATE SKIP
  LOCKED` + the `swept_at` stamp) and the nudges are two transactions: the
  system session is not a Unit of Work, so the nudges go through the outbox
  of an ordinary UoW right after (a lost nudge is re-driven next interval).
  An idle tick on a `tenant_rls` database rolls its system session back, so
  it leaves no `sandloom.system_db.session` audit row (only its log lines). The
  sweep also refreshes the `sandloom_saga_instances` gauge about once a minute and
  purges terminal instances older than `SAGA_RETENTION_DAYS` about once an
  hour (parked instances are kept). Without a cache that supports `cas` the
  sweep runs unleased (safe, only redundant). The lease is
  `sandloom.core.beat_lease.FencedLease`, the beat lease's state machine made
  async and shared.
- **A6 — the system channel is injected (§2, §10).** `sandloom.core.sagas` sits
  in the contracts & RPC layer and may not import a concrete driver
  (`test_concrete_drivers_imported_only_in_composition_layer`), so
  `create_app` passes `sandloom.core.drivers.system_db`'s `system_session` to
  `install_sagas(core, system_channel=...)`. Startup refuses a saga database
  with `tenant_rls` and no system channel. The core migration enables
  `system_access` only when the `sandloom_system` role exists (an RLS deployment
  creates it first).
- **A7 — remote providers need a snapshot (§1, §14).** The runtime injects a
  command's idempotency argument by name, so it must know the name for a
  remote provider at startup: a call to a contract that is neither local nor
  covered by a snapshot (`contracts.snapshot.json` in the provider's package
  or `CONTRACT_SNAPSHOT_PATHS`) is a `SagaDefinitionError` with a fix, not a
  deferred check.
- **A8 — classification details (§7).** A `ServiceNotFound` that is not the
  `501` deployment gap is `misconfigured`. A local step refuses by raising an
  exception whose `http_status` is in the refused set; any other exception is
  `transient` and logged with its traceback. An `args` function that raises
  parks the instance as `misconfigured` (code `saga_args_failed`): a
  deterministic failure never heals by retrying.
- **A9 — operator semantics (§8).** `retry` also grants a fresh window
  (`deadline_at` for a forward instance, `compensation_deadline_at` for a
  backward one) — otherwise an instance parked by a deadline would park
  again on the next tick; the post-TTL rule still guards the re-call.
  `resolve` applies to a parked instance's current step: forward `applied` →
  the next step; forward `skipped` → the next step without the effect
  (never compensated); for a compensation (or the ambiguous step of a
  compensating instance) `applied` / `skipped` record the outcome and the
  saga continues from there. `resolve` continues the instance at once (no
  separate `retry`).
- **A10 — unknown versions.** Besides parking at advance time, a
  non-critical startup task logs the (type, version) of active instances
  that no registered definition covers, and `sandloom doctor` warns about them.
- **A11 — per-version snapshot (§11)** is phase 2, as the phased plan says;
  phase 1 does not pin step names under a `sagas` key yet.
- **A12 — test seams (§14).** `saga_harness.drive()` acts as the timer sweep:
  with no nudge pending it advances its clock to the instance's next due time
  (jitter fixed at 0.5). `crash_before(step)` joins `crash_after`; the two
  one-liners take `input=` and `tenant_id=` (a real provider needs real input
  and, when tenant-scoped, a tenant).
- **A13 — the worked example** is a new app, `examples/academy` (payments,
  notifications, enrollment); its saga reserves the seat in a local
  compensatable step first, so a refused charge cancels the seat.

### Measurements (Consequences)

`benchmarks/ladder/saga_rung.py`, statements pinned by
`benchmarks/tests/test_saga_rung.py`; wall times from one run on a shared
dev machine (load ≈ 3–4, n = 1000), indicative only:

| | statements | median |
| --- | --- | --- |
| business UoW (baseline) | 1 | 1.25 ms |
| + `start_saga` | +3 (instance INSERT, outbox nudge, its post-commit mark) | +2.3 ms |
| one local step's transition (= connection hold) | 5 (lock, step upsert, instance update, outbox nudge, its post-commit mark) | 5.0 ms |
| one command step (local provider) | 7 (+ the command log's claim INSERT and result UPDATE) | 7.0 ms |

The "4 statements per step" of the Consequences holds; the fifth is the
existing outbox mark-delivered of the nudge. The single-transaction step
(§6, question 6) stays: the hold is one step call, measured in milliseconds.

## Alternatives rejected

- **Holding the transaction across the whole saga, or 2PC/XA:** forbidden by
  ADR-0005 and by the split deployment model.
- **Event-sourced saga state** (replaying a step journal, as Temporal, Azure
  Durable Functions and Restate do): it imposes determinism rules on module
  code and a journal store, which is a new dedupe and step store, for no gain
  over a version-guarded row with persisted results.
- **Timers as delayed outbox rows (`deliver_at`, review C2 #12):** viable, and
  still useful for plain delayed events. For sagas the deadline belongs on the
  instance row, because a cancelled timer must not need a delete.
- **Celery ETA/countdown for timers:** these messages are held in worker memory
  and are subject to broker visibility timeouts and redelivery quirks.
- **A DLQ for stuck sagas:** a parked status on the instance is visible,
  queryable and audited, while a DLQ entry is not.

## Worked example placement

The research document's org-setup example needs four new commands in the
bundled modules, an `org_owner` platform role in identity (tenancy's
membership already records the owner), and makes tenancy consume billing and
identity — which would break `test_module_boots_alone` for a demo. Granting
trial credits on org creation is also a plain fan-out that choreography
handles (billing reacts to `tenancy.organization_created.v1`).

So the MVP's worked example lives in an **example app** (a course-enrollment
flow in the shape of §14: charge → grant access as the pivot → notify as a
retriable step), with its own modules and contracts, and runs in the split
conformance matrix. The bundled modules gain saga-ready commands only when a
real flow needs them.

## Open questions (owner) — with the recommended answers

1. **Who owns org onboarding?** Recommended: nobody yet. Trial credits are
   choreography; a real onboarding saga, when one appears, belongs to a
   separate module started by `tenancy.organization_created.v1`, so tenancy
   stays free of billing and identity.
2. **Timer sweep in the relay leader or its own loop?** Decided in §5: its
   own lease-guarded loop in every process hosting a saga module, so dev and
   prod drive sagas the same way.
3. **Default `compensation_deadline`?** 1 h, as an **ops** setting
   (`SAGA_COMPENSATION_DEADLINE_SECONDS`), overridable per definition; the
   window check covers it.
4. **Durable inbox on the advance consumer?** Decided in §5: no. Advances are
   versioned nudges; the row lock and version are the guard.
5. **Billing's intent request path?** Stays hand-rolled permanently (its pivot
   is the request's own commit and its result is a bearer secret); only the
   reconciler is a phase-3 candidate.
6. **One transaction per step?** Decided in §6: yes, measured by a benchmark
   rung; the two-transaction variant only if the measurement demands it.
7. **When to build phase 1?** Decided: **build now** (owner, 2026-10-25).
   Phase 0 and the phase-1 MVP ship together; the worked example is the
   course-enrollment flow the Tutor product needs (payment → course access →
   notifications).
