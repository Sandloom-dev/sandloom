---
title: Add a saga
description: Run a multi-step process across modules with compensation, starting from `sandloom add saga`, then start it from a route and test it with the saga harness.
type: how-to
---

# Add a saga

**Goal:** a linear process that spans modules, such as "charge, then grant
access, then notify", where a failure before the go/no-go point undoes the
earlier steps and a failure after it is retried until it succeeds.

**Use it** when a later failure must undo an earlier module's effect, one
module owns the process, and an operator must be able to see and repair a
stuck instance.

**Do not use it** when the change touches one module's tables (one unit of
work is enough), or when nothing has to be undone (publish an
[event](add-event.md) and let other modules react). Read
[Sagas](../concepts/sagas.md) first: a saga has no isolation, and you must
design for the intermediate states other requests see.

## Prerequisites

- The owning module exists ([Add a module](add-module.md)).
- The command contracts the steps call exist, with idempotent compensation
  commands that accept "nothing to undo"
  ([Add a contract](add-contract.md)). The generator gives you a stand-in so
  the saga runs before they exist.

## Steps

### 1. Run the generator

```bash
sandloom add saga notes onboarding
```

It writes:

- `<module>/sagas.py` with the definition (written as `sagas.py.new` if the
  file exists);
- a stand-in command contract `notes.onboarding_ledger.v1`, appended to
  `<module>/contracts.py` with its DTO in `<module>/schemas.py`, and its stub
  and DTO copy in the shared DTO package, exactly as
  [`sandloom add contract`](add-contract.md) does. The saga's
  command step calls it, so the generated code boots and runs.

It writes no tests and no route: you add both below.

### 2. Read what it wrote

```python
--8<-- "docs/snippets/generated/saga/demo/modules/notes/sagas.py"
```

- `ledger = Contract(NOTES_ONBOARDING_LEDGER_V1)` declares the command
  contract the steps call. `call=ledger.record` names the command through
  the typed reference, so a renamed method is a type error.
- `reserve` is a **command step**: it calls the command exactly once at the
  provider (through its durable command log, keyed by the runtime) and names
  its compensation.
- `confirm` is a **local step** marked `kind="pivot"`: it runs in the
  transition's own transaction, and it is the go/no-go point. Before it, a
  refusal compensates; after it, steps are retried until they succeed.
- `deadline=timedelta(minutes=10)` bounds the forward run.

The stand-in provider (`contracts.py` of a module that had none):

```python
--8<-- "docs/snippets/generated/saga/demo/modules/notes/contracts.py"
```

### 3. Replace the stand-in with your process

1. Point each command step at the real provider: import its generated
   reference, declare `x = Contract(X_Y_V1)` at module level, and use
   `call=x.<command>` and `compensate=x.<undo command>`.
2. Delete the stand-in class from `contracts.py`, its DTO
   (`OnboardingLedgerStatus`) from `schemas.py` and its stub from the shared
   DTO package (`notes_onboarding_ledger_v1.py`), then run
   `sandloom generate` (it rewrites the DTO copies), before anything calls them.
3. Rewrite the input model. It holds ids and amounts, never personal data:
   it is stored on the instance row.
4. Keep every `args` / `compensate_args` function pure, named and at module
   level: it reads only `ctx.input` and `ctx.result(...)`. Arguments are
   recomputed on every attempt; a drift parks the instance.
5. Choose the kinds: compensatable steps first, at most one `pivot`, then
   retriable steps. Use `ctx.key("<step>")` to pass a step's operation key to
   its compensation.
6. Guard user-visible intermediate states with a status column that your
   routes respect until the saga decides (a semantic lock).

Changing the steps of a definition that already has running instances is a
new `version=`, registered alongside the old one.

### 4. Start it from a route

`start_saga` writes the instance and its first nudge in the caller's unit of
work, so the saga starts only if the business change commits. Answer
`202 Accepted` with a URL to poll:

```python
from sandloom.sdk.sagas import start_saga, saga_status

handle = await start_saga(ONBOARDING, saga_id=row.id, input=OnboardingInput(...))
```

Starting the same `saga_id` again with equal input returns the existing
instance; other input raises `SagaInputConflict` (409).
`saga_status(ONBOARDING, saga_id)` is the read side. See
[Sagas](../concepts/sagas.md#starting-one-202-accepted) for a full route.

## Verify

The `saga_harness` fixture of the testing plugin drives the real saga
protocol in a test, synchronously, and injects faults:

```python
async def test_refund_when_confirm_refuses(saga_harness) -> None:
    saga_harness.fail_step("confirm", error="refused")
    run = await saga_harness.run(ONBOARDING, input=OnboardingInput(owner_id=uuid4(), amount=5))
    assert run.status == "compensated"
```

Every saga's test module should also contain the two one-liners
`await saga_harness.assert_compensations_idempotent(ONBOARDING, input=...)`
and `await saga_harness.assert_resolves_before_compensating(ONBOARDING,
input=...)`. Then:

```bash
sandloom test notes
```

In a running deployment, `sandloom sagas list` shows the instances that need
attention and `sandloom sagas show <id>` the timeline and the remediation of a
parked one.

## Errors you may hit

- [SANDLOOM-B005](../reference/error-codes.md#sandloom-b005): a step's contract is
  neither provided in the process nor bound in `SERVICE_BINDINGS`.
- [SANDLOOM-B006](../reference/error-codes.md#sandloom-b006): the saga is defined
  outside the module's `sagas` submodule.
- A startup refusal naming the saga, the step and a fix: a step's call or
  `args` does not match the provider's describe document.
- [SANDLOOM-B018](../reference/error-codes.md#sandloom-b018): a command the saga calls
  has no `idempotency_key`.
- [SANDLOOM-R002](../reference/error-codes.md#sandloom-r002): `start_saga` ran outside
  a unit of work.

## Related

- [Sagas](../concepts/sagas.md): kinds, park reasons, countermeasures for
  the missing isolation.
- [Add a contract](add-contract.md): the command contracts a saga calls.
- [Contracts](../concepts/contracts.md): command dedupe.
- [Operations runbook](../operations/runbook.md): repairing parked
  instances.
