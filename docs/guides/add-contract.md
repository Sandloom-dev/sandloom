---
title: Add a contract
description: Provide a synchronous cross-module API with `sandloom add contract`, author its DTOs in the provider, generate its typed stub, and call it with `Contract(REF)`.
type: how-to
---

# Add a contract

**Goal:** module A answers a synchronous question or runs a command for
module B, through a versioned contract such as `stats.quota.v1`, with a typed
stub that B imports instead of A's code.

**Use it** when the caller needs an answer now: a quota check, a lookup, a
command that must succeed or fail before the caller goes on.

**Do not use it** when the caller only needs to tell others that something
happened (publish an [event](add-event.md)), or to read many rows of another
module for a list page (build a [projection](add-projection.md)). A contract
call runs in the provider's own unit of work, never in the caller's
transaction: a command is not atomic with the caller's write. A multi-step
change with undo is a [saga](add-saga.md).

## Prerequisites

- The providing module exists ([Add a module](add-module.md)).
- If `contracts.py` already exists, it must parse and import
  `command, contract` from `sandloom.sdk`.

## Steps

### 1. Run the generator in the providing module

```bash
sandloom add contract stats quota
```

The contract name is `<module>.<name>.v1`, here `stats.quota.v1`. The
generator writes, inside the module package:

- the provider class, **appended** to `<module>/contracts.py`;
- its DTO, a pydantic model **appended** to `<module>/schemas.py`: a provider
  authors the models and Enums its signatures use in its own `schemas`.

Then `sandloom add` runs `sandloom generate`, which writes the generated files
in the shared DTO package:

- the typed stub (`<app>/shared/contracts/stats_quota_v1.py`), rendered from
  the provider;
- a wire-shape copy of the module's contract DTOs
  (`<app>/shared/contracts/stats_dto.py`), which the stub names and callers
  receive.

Generated files are never edited by hand; `sandloom generate` rewrites them.

### 2. Read what it wrote

The provider, with two stand-in methods. A public `async def` is a **read**;
`@command(idempotency_key=...)` marks a **command**, deduplicated by that
argument and committed in the provider's own unit:

```python
--8<-- "docs/snippets/generated/contract/demo/modules/stats/contracts.py"
```

The stub callers import: a `Protocol` and a typed reference to it, which
names the DTO's generated copy:

```python
--8<-- "docs/snippets/generated/contract/demo/shared/contracts/stats_quota_v1.py"
```

The copy carries the fields, their types and constraints, defaults, aliases,
`Literal` / `Enum` values, nested models and the `model_config` options; not
methods or validators (they stay the provider's):

```python
--8<-- "docs/snippets/generated/contract/demo/shared/contracts/stats_dto.py"
```

### 3. Write the real methods

Replace the stand-ins with your API. The class stays stateless: no
`__init__`, no instance attributes (`ClassVar` constants are allowed). Every
parameter and return type must be a wire type; models and Enums go in your
module's `schemas`, inside the closed set of wire shapes
([Contracts](../concepts/contracts.md#wire-types-and-dtos)). A caller
receives the generated copy, locally and over RPC; the provider receives its
own class.

The tutorial app's `stats` module provides one read:

```python
--8<-- "examples/tutorial/tasklist/modules/stats/contracts.py:contract"
```

### 4. Regenerate the stub

`sandloom generate` regenerates the stub and the DTO copies from the provider.
It compares the new stub with the file on disk first: an added method is
written, but a removed or changed method is refused (SANDLOOM-T007), because
callers rely on it, and nothing is written. `sandloom test` never writes: it
fails on a stale file ("run `sandloom generate`").

Replacing the generator's stand-ins removes `status` and `record`. While no
caller uses the contract yet and the stub is not committed, delete the stub
file and generate it again:

```bash
rm <app>/shared/contracts/stats_quota_v1.py
sandloom generate stats
```

The result in the tutorial app:

```python
--8<-- "examples/tutorial/tasklist/shared/contracts/stats_quota_v1.py"
```

Once the contract is committed and called, change it only compatibly (add
methods); a breaking change is a new contract, `stats.quota.v2`.

### 5. Call it from another module

Declare the caller once, at module level, with the generated reference.
`define_module` adds it to the module's `consumes`, and startup fails if the
contract is neither provided in the process nor bound to a remote service
(SANDLOOM-B005):

```python
--8<-- "examples/tutorial/tasklist/modules/tasks/api.py:quota"
```

Then await its methods. The call is typed by the `Protocol`:

```python
--8<-- "examples/tutorial/tasklist/modules/tasks/api.py:routes"
```

The call works the same whether the provider runs in the same process or in
another service behind RPC (configured with `SERVICE_BINDINGS`; see
[Service RPC](service-rpc.md)). A provider that is deployed but down answers
`503`.

### 6. Required or optional

`Contract(REF)` is **required**. Use it whenever the caller has no correct
answer without the provider. A gate whose absence must deny, such as the
quota above, is always required: an absent quota service must not silently
allow writes.

`Contract(REF, optional=True)` is for the rare caller that has a correct
business answer without the provider, such as a feature that is off or an
enrichment that is skipped:

```python
recommendations = Contract(RECO_ITEMS_V1, optional=True)

client = recommendations.client()   # RecoItemsV1 | None
extra = [] if client is None else await client.for_item(item_id)
```

`client()` returns `None` only when the provider is neither local nor bound
in this deployment, which is decided at startup. It does not hide an outage:
a deployed provider that fails still raises.

## Verify

Test the caller with a stand-in for the contract, so the test does not need
the provider:

```python
--8<-- "examples/tutorial/tasklist/modules/tasks/tests/test_tasks.py:tests"
```

Then run both modules' tests and the gates:

```bash
sandloom test stats
sandloom test tasks
```

`sandloom generate` also writes `contracts.snapshot.json` for the provider;
commit it with the stub and the DTO copy.

## Errors you may hit

- [SANDLOOM-B005](../reference/error-codes.md#sandloom-b005): a required contract is
  neither provided in this process nor bound in `SERVICE_BINDINGS`.
- [SANDLOOM-B018](../reference/error-codes.md#sandloom-b018): the provider class has
  state, a non-async member, a `@command` without `idempotency_key`, or a
  base class outside the module.
- [SANDLOOM-C012](../reference/error-codes.md#sandloom-c012): a signature DTO is
  outside the closed set of wire shapes (the field path and the fix are
  named), belongs to another module or a function, or its generated copy is
  missing or hand-edited (run `sandloom generate`).
- [SANDLOOM-R005](../reference/error-codes.md#sandloom-r005): the contract is not
  available in this deployment at call time.
- [SANDLOOM-T004](../reference/error-codes.md#sandloom-t004): the provider's contract
  snapshot changed incompatibly.
- [SANDLOOM-T007](../reference/error-codes.md#sandloom-t007): the stub is out of date,
  or would lose or change a method.

## Related

- [Contracts](../concepts/contracts.md): reads, commands, wire types, local
  equals remote.
- [Service RPC](service-rpc.md): calling a provider in another service.
- [Add a saga](add-saga.md): command steps with compensation.
- [Testing modules](testing.md).
