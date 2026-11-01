---
title: Sagas
description: Cross-module processes with compensation - when you need one, typed steps, operation keys, recovery rules and the missing isolation.
type: concept
---

# Sagas

A write that spans modules is a saga by construction. A command contract runs
in the **provider's** own unit, never in the caller's transaction, and there
is no distributed transaction across databases. A **saga** is a linear,
versioned process owned by one module: a list of steps, each a command
contract call (`step`) or a local write (`local_step`), with compensations
before a go/no-go point (the pivot) and forward recovery after it. Sandloom keeps
its state in the owning module's database, drives it one step per
transaction, retries, compensates or parks it by an explicit table, and gives
operators `sandloom sagas ...` to inspect and move stuck instances. A saga is for
a process with **undo**; a fan-out without undo is choreography with
[events](events.md).

## Do you need one?

```text
Does the change touch ONE module's tables?
├── yes ─► a plain unit of work (db()). Done.
└── no ─► Must a later failure UNDO an earlier module's effect?
          ├── no ─► choreography: publish an event, react with an @on consumer
          │         (durable inbox, or a command keyed by the event). Fan-outs:
          │         notifications, erasure, cache invalidation, "grant trial
          │         credits when an organization is created".
          └── yes ─► Is there ONE owner that must know "where is this process"
                     and an operator who must see a stuck one?
                     ├── yes ─► a saga (this page).
                     └── no ─► rethink the boundary: one invariant belongs in
                               ONE module and ONE database.
```

## What you get by default / what you declare

| You get by default | You declare |
| --- | --- |
| Discovery and registration from the module's `sagas.py`; every called contract added to `consumes` | `Saga(name, version=, input=, deadline=, steps=[...])` |
| A startup check of every step against the provider's describe document (the method exists, is a command, the `args` names match) | `step("s", call=<caller>.<command>, args=fn, compensate=..., compensate_args=fn)` |
| A derived operation key per step, `saga:<type>:<id>:<step>:do` / `...:undo`, passed to the command | nothing (never pass the key yourself) |
| Step kinds defaulted around the pivot | `kind="pivot"` on the go/no-go step |
| Instance row and first nudge written in the caller's unit: the saga starts iff the business change commits | `await start_saga(DEF, saga_id=<business id>, input=...)` |
| Retries with full-jitter backoff, compensation in reverse, parking with an audit row, a timer sweep for deadlines | `deadline=`, `compensation_deadline=`, per-step `deadline=` |
| Operator commands, metrics and `sandloom doctor` warnings | nothing |

## How to use it

### The shape

```python
# <module>/sagas.py
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from sandloom.sdk import Contract
from sandloom.sdk.sagas import Saga, SagaContext, local_step, step
from myapp.shared.contracts.payments_charges_v1 import PAYMENTS_CHARGES_V1
from myapp.shared.contracts.notifications_mail_v1 import NOTIFICATIONS_MAIL_V1

from .services import enrollments

charges = Contract(PAYMENTS_CHARGES_V1)
mail = Contract(NOTIFICATIONS_MAIL_V1)


class EnrollInput(BaseModel):
    enrollment_id: UUID
    student_id: UUID
    price: int


def charge_args(c: SagaContext[EnrollInput]) -> dict[str, Any]:
    return {"customer_id": c.input.student_id, "amount": c.input.price}


def refund_args(c: SagaContext[EnrollInput]) -> dict[str, Any]:
    return {"customer_id": c.input.student_id, "charge_key": c.key("charge")}


def notify_args(c: SagaContext[EnrollInput]) -> dict[str, Any]:
    return {"user_id": c.input.student_id, "template": "enrolled"}


async def grant_access(session: Any, c: SagaContext[EnrollInput]) -> str:
    return await enrollments.activate(session, c.input.enrollment_id)


ENROLL = Saga(
    "enroll", version=1, input=EnrollInput, deadline=timedelta(minutes=10),
    steps=[
        step("charge", call=charges.charge, args=charge_args,
             compensate=charges.refund, compensate_args=refund_args),
        local_step("grant_access", kind="pivot", apply=grant_access),
        step("notify", call=mail.send, args=notify_args),
    ],
)
```

- **Typed steps.** `call=charges.charge` names the command through the
  caller's typed reference, so a renamed method is a type error. It stands
  for the string `"payments.charges.v1.charge"`, which the L2 form takes;
  both forms register the same definition.
- **Command steps** call a command contract (exactly once at the provider,
  through its durable command log). **Local steps**,
  `local_step(apply=fn, compensate=fn)`, run `await fn(session, ctx)` in the
  transition's own transaction (a savepoint): their writes commit with the
  instance's move to the next step. A local step refuses definitively by
  raising an exception whose `http_status` is 400, 402, 403, 404, 410 or 422;
  anything else is retried. Its JSON-able return value is stored as the
  step's result.
- **Kinds.** `compensatable` (before the pivot; needs a compensation),
  `pivot` (the go/no-go point, at most one), `retriable` (after the pivot; it
  must eventually succeed). A step usually names only `call`, `args` and its
  compensation.
- **Typed context.** `c.input` is the definition's input model;
  `c.result("charge", int)` decodes a step's result; `c.key("charge")` is a
  step's operation key (pass the charge's key to the refund).
- **Other options** of `step`: `refuse_on=` (extra statuses treated as
  refusals, for example `{409}` for a provider whose domain conflict is a
  real refusal; it may not widen a transient status), `check_args=False` (opt a step out of the startup argument
  check), `deadline=` (a per-step deadline).
- **Startup checks.** The module does not boot without its providers, local
  or bound in `SERVICE_BINDINGS`. Every call is checked against the
  provider's describe document (the local provider's, or the snapshot shipped
  with its package for a remote one), and each `args` function runs once on a
  synthetic context to check its names. Errors name the saga, the step and a
  fix.

`sandloom add saga <module> <name>` writes a definition, its input model and a
stand-in ledger contract with its stub; it writes no tests and no route. See [Add a saga](add-saga.md).

### Rules for authors

- `args` / `compensate_args` are **pure, named, module-level functions** of
  `c.input` and `c.result(...)`: no lambdas, no closures over request state,
  no clock, no randomness. Arguments are recomputed on every attempt; a
  drift hits the command log's argument digest (409) and parks the instance.
- Every compensatable step names a compensation **command** that is
  idempotent and **accepts "nothing to undo"**: refunding a charge that never
  happened is a success, not a 404 (the forward call may have been refused,
  or never reached the provider). Its key is `...:undo`; a repeat replays
  through the command log.
- Steps after the pivot are retriable and never refuse.
- `input` and results hold ids and amounts, never PII: they are stored on the
  instance row.
- Guard a user-visible intermediate state with a **semantic lock** (a status
  column the module's routes respect until the saga decides). Never validate
  a strong invariant against another saga's intermediate state.
- A definition change (steps, kinds, calls) is a new `version=`, registered
  alongside the old one while old instances run.
- `(step deadline, else saga deadline) + compensation_deadline` must fit
  `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`; production refuses otherwise (risk
  `saga_horizon_beyond_command_dedupe`).

### Starting one: 202 Accepted

```python
from sandloom.sdk import CurrentUser
from sandloom.sdk.sagas import start_saga

@router.post("/enrollments", status_code=202)
async def enroll(body: EnrollIn, user: CurrentUser) -> EnrollAccepted:
    row = await enrollments.create(user.user_id, body.course_id, body.price)
    handle = await start_saga(
        ENROLL, saga_id=row.id,
        input=EnrollInput(enrollment_id=row.id, student_id=user.user_id, price=body.price),
    )
    return EnrollAccepted(status=handle.status, status_url=f"/api/v1/enrollments/{row.id}")
```

`start_saga` writes the instance row and its first nudge to the outbox **in
the caller's unit**: the saga starts iff the business change commits. The
same `saga_id` again is a create-or-get (equal input returns the existing
handle with `created=False`; other input raises `SagaInputConflict`, 409).
Outside a unit it raises `UnitOfWorkRequiredError`. `saga_status(ENROLL,
saga_id)` is the read side (`None` if never started). The route gets the
ordinary write defaults (authentication, the session check, automatic
idempotency); nothing to declare.

### How it runs

Each saga-owning module gets a kernel consumer `<module>:sagas@advance` on
its private channel `<module>.saga_advance.v1`. A nudge carries
`{instance_id, expected_version}`; the handler locks the instance row
(`FOR UPDATE SKIP LOCKED`), does nothing unless it is active, due and still at
that version, runs **one** step, writes the transition (version + 1) and
publishes the next nudge: one transaction per step. A lease-guarded **timer
sweep** in every process hosting a saga module re-drives instances whose
backoff elapsed or whose deadline passed (`SAGA_SWEEP_INTERVAL_SECONDS`),
independent of the outbox relay, so development runs sagas exactly as
production does.

| A step call... | before / at the pivot | after the pivot | compensating |
| --- | --- | --- | --- |
| succeeds | next step | next step | previous step |
| refuses (400/402/403/404/410/422 from domain code) | **compensate** | park (`retriable_refused`) | park (`compensation_refused`) |
| fails transiently (408/423/425/429/5xx, timeouts, anything unknown) | retry with full-jitter backoff until the deadline, then compensate | retry until the deadline, then park (`deadline_exceeded`) | retry until `compensation_deadline`, then park (`compensation_deadline_exceeded`) |
| conflicts (409: same key, other arguments) | park (`nondeterministic_args`) | park | park |
| is miswired (401, argument binding errors) | park (`misconfigured`) | park | park |

**Resolve before compensating.** A step whose last call was ambiguous (a
timeout: the provider may have committed) is never compensated blindly. The
runtime re-issues the forward call with the same key first (the command log
replays it, waits for it or runs it) and compensates only an applied step. A
parked instance never moves without an operator.

### Park reasons

`sandloom sagas show` prints the reason and the remediation of a parked instance:

| Reason | What to do |
| --- | --- |
| `retriable_refused` | a step after the pivot refused: fix the provider or the arguments, then `sandloom sagas retry`; if the effect is not needed, `sandloom sagas resolve --step S --as skipped` |
| `compensation_refused` | undo the effect by hand at the provider (search for the step's operation key), then `sandloom sagas resolve --step S --as applied` |
| `compensation_deadline_exceeded` | fix the provider, then `sandloom sagas retry` |
| `deadline_exceeded` | after the pivot, forward recovery only: fix the failing provider, then `sandloom sagas retry` (grants a fresh deadline) |
| `nondeterministic_args` | the `args` function is not pure: fix it, ship a new definition version, then resolve the step by hand |
| `result_not_replayable` | the step ran but its result is too large to replay: establish the outcome at the provider, then `sandloom sagas resolve --step S --as applied` |
| `misconfigured` | fix the binding or the `args` function and deploy, then `sandloom sagas retry` |
| `unknown_definition` | deploy the version that started it (registered alongside the new one), then `sandloom sagas retry` |
| `outcome_unknown_after_ttl` | the last attempt was ambiguous and its dedupe record may have expired: establish the outcome at the provider, then `sandloom sagas resolve --step S --as applied` or `--as skipped` |
| `operator` | parked by an operator: `sandloom sagas retry` when it may continue |

### Operation keys

Every command a flow issues more than once for the same business fact needs a
key derived from business ids, never a random one:

```python
from sandloom.sdk.effects import operation_key

operation_key("refund", order_id, line_no)      # "refund:<order_id>:<line_no>"
```

Parts are non-empty and contain no `:` or whitespace, so two different tuples
never collide. Saga steps get theirs from the runtime.

### Countermeasures for the missing isolation

A saga is ACD without the I: other requests see intermediate states. Apply
countermeasures explicitly (Richardson, *Microservices Patterns*, ch. 4):

| Countermeasure | What you do | In Sandloom |
| --- | --- | --- |
| **semantic lock** | a status column (`pending`, `reserved`) that other operations respect until the saga decides | the enrollment row: `pending -> reserved -> active / cancelled`; routes refuse or show "processing" |
| **commutative updates** | design updates so their order does not matter (credit/debit, not "set balance") | ledger entries instead of overwrites |
| **pessimistic view** | reorder steps so risky updates happen in retriable steps after the pivot | notify or pay out after the pivot |
| **reread value** | re-check a value before acting on it (optimistic version check) | billing's operation intents re-read before releasing |
| **version file** | record operations so out-of-order ones can be reordered | a per-aggregate `version` in event payloads |
| **by value** | choose the mechanism by business risk | a saga for payments, choreography for emails |

### Recipes

- **Reserve, charge, confirm.** A local compensatable step takes the semantic
  lock (`reserved`; compensation: `cancelled`), the charge is a command with a
  refund, the confirmation is the local pivot. The academy example
  (`examples/academy`) is exactly this.
- **A payment provider call** is a local step that keys its own idempotency
  on `c.key(step)` (the provider's idempotency header), as billing's intents
  do.
- **Notify after the pivot.** A retriable command step; a failing mail
  service retries until the deadline, then parks for an operator.
- **Organization onboarding** without undo is choreography: react to the
  organization-created event in the module that grants trial credits.

### Anti-patterns

- A lambda, or a closure over request state, as `args`.
- PII in the input or results.
- A retriable step that refuses: after the pivot a refusal parks the
  instance.
- A compensation that answers 404 on "nothing to undo": it parks the saga.
- Holding a row lock a step will take: the step runs in another transaction
  (the contract pool, or another process).
- Editing a registered definition in place: ship a new `version`.
- A random operation key: a retry would run the effect again.
- Long deadlines beyond the command-dedupe TTL.

### Testing

```python
async def test_refund_when_access_cannot_be_granted(saga_harness):
    saga_harness.fail_step("grant_access", error="refused")
    run = await saga_harness.run(ENROLL, input=...)
    assert run.status == "compensated"
    assert run.calls == ["charge:do", "grant_access:do", "charge:undo"]
    run.assert_keys_derived()
```

`saga_harness` (a fixture of the `sandloom.sdk.testing` plugin) drives the real
protocol against the test database: `run()` / `drive()`,
`fail_step(..., error="transient" | "refused" | "timeout")`, `crash_before` /
`crash_after`, `deliver_twice()`, `clock.advance(timedelta)`,
`fake_contract(name, **fns)` (still through the command log), and the
one-liners `assert_compensations_idempotent(DEF)` /
`assert_resolves_before_compensating(DEF)`.

### Operating

```bash
sandloom sagas list                               # parked, compensating, overdue
sandloom sagas show enroll <id>                   # timeline, why parked, what to do
sandloom sagas retry|park enroll <id> --apply --reason "..."
sandloom sagas resolve enroll <id> --step charge --as applied --apply --reason "..."
```

Operator mutations are dry runs unless `--apply`, and are audited. Metrics:
`sandloom_saga_instances{saga_type,status}`, `sandloom_saga_step_duration_seconds`,
`sandloom_saga_step_attempts_total{outcome}`, `sandloom_saga_parked_total{saga_type,reason}`
(alert on it). `sandloom doctor` warns about parked and overdue instances; sizing
and the runbook are in `docs/operate/operator-reference.md` ("Sagas") in the repository.

## What breaks it

| Code or error | When |
| --- | --- |
| `SagaDefinitionError` | at registration: duplicate steps, a missing compensation, steps on the wrong side of the pivot, more than one pivot, a call that is not a command, `args` names that do not match the command |
| [SANDLOOM-B005](../../reference/error-codes.md#sandloom-b005) | a called contract is neither provided locally nor bound |
| [SANDLOOM-B006](../../reference/error-codes.md#sandloom-b006) | a `Saga` declared outside the module's `sagas` submodule |
| [SANDLOOM-R002](../../reference/error-codes.md#sandloom-r002) | `start_saga` outside a unit |
| `SagaInputConflict` (409) | the same `saga_id` started with other input |
| `GuaranteeWindowError` | in production, a deadline horizon beyond `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS` without the accepted risk |

??? note "Guarantees, precisely"
    - **Definition (G-SAGA-01, 02, 14).** Malformed definitions are refused at
      registration; every called method is a consumed contract; an instance of
      an unregistered version parks instead of crashing.
    - **Start (G-SAGA-05).** Transactional with the business change,
      create-or-get on `saga_id`.
    - **Steps (G-SAGA-04, 06, 07).** Each command step carries its derived
      key; duplicate, stale or concurrent nudges never run a step twice; a
      crash before the call or after the provider committed converges without
      re-applying the step (same key, command-log replay). Exactly once holds
      at the provider only within the command-dedupe TTL.
    - **Recovery (G-SAGA-09 to 12).** A closed outcome table; compensation in
      reverse before the pivot; parking with an audit row after it; resolve
      before compensate; no blind re-call after the TTL.
    - **Horizon (G-SAGA-13).** Production refuses a horizon beyond the
      command-dedupe TTL unless the risk is accepted.
    - **Driving and state (G-SAGA-03, 08, 15).** The timer sweep runs at most
      once per interval; instance tables are reached across tenants only
      through the audited system channel; operator mutations are audited.
    - **Not provided:** isolation (see the countermeasures), parallel steps,
      a distributed transaction.

    See [ADR-0008](../../adr/0008-sagas.md) and its amendment of
    [ADR-0005](../../adr/0005-effect-model.md), and the
    [guarantee ledger](../../reference/guarantees.md) (C-SAGA-01 to 03 for the
    capabilities).

## Design rationale

- [ADR-0008: Sagas](../../adr/0008-sagas.md): the orchestrator, its state, the
  step protocol and the recovery rules.
- [ADR-0010: Public API 1.0](../../adr/0010-public-api-1.0.md) §3: typed saga
  steps.
