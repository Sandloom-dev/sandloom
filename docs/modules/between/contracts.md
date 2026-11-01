---
title: Contracts
description: Synchronous cross-module calls - @contract providers, @command, Contract(REF) callers, wire types, command dedupe and local-equals-remote.
type: concept
---

# Contracts

A **contract** is a versioned, synchronous API that one module provides and
other modules call, such as `stats.quota.v1`. The provider is a stateless
class decorated with `@contract("stats.quota.v1")`; its public `async def`
methods are **reads**, and methods marked `@command(...)` are **commands**.
A caller declares `quota = Contract(STATS_QUOTA_V1)` at module level, using a
typed reference that `sandloom generate` writes, and awaits `quota.method(...)`.
Every call runs in the **provider's** own unit on the provider's database,
never in the caller's transaction, so it behaves the same whether the
provider is in the same process or in another service behind RPC. Use a
contract when you need an answer now; use an [event](events.md) when the
other module only needs to know that something happened.

## What you get by default / what you declare

| You get by default | You declare |
| --- | --- |
| Registration of the provider; every public `async def` becomes a read | `@contract("<module>.<name>.v1")` on a class with no state |
| Reads run in a provider unit that is always rolled back, memoised per request | nothing |
| A command runs in its own provider unit that commits before returning, deduplicated by a durable log in the provider's database | `@command(idempotency_key="<param>")` naming the parameter that carries the key |
| Argument binding, validation and result decoding through one wire codec, identical locally and over RPC | wire-type annotations on every parameter and the return |
| The typed stub `X_Y_V1: ContractRef[Protocol]` in the shared DTO package, written by `sandloom generate` | nothing (never edit the stub) |
| A wire-shape copy of each DTO a signature names, next to the stub (`<module>_dto.py`), which the caller receives | the DTO, in the provider's own `schemas` module |
| The caller's `consumes` entry, with the methods it calls, derived from the module-level `Contract(...)` | `quota = Contract(STATS_QUOTA_V1)` |
| Startup refusal when a required provider is neither local nor bound | `Contract(REF, optional=True)` only when "no provider" has a correct business answer |
| Local resolution first, then the RPC proxy from `SERVICE_BINDINGS` | the binding, in a split deployment |
| A compatibility snapshot of the provider's describe document | a new `.v2` contract for a breaking change |

## How to use it

### Provide

Put the provider in the module's `contracts.py`; discovery registers it.

```python
--8<-- "examples/tutorial/tasklist/modules/stats/contracts.py:contract"
```

- **Stateless.** No `__init__`, no instance attributes; `ClassVar` constants
  are allowed. Every public member is an `async def` contract method; helpers
  are private (`_name`). Inherited methods count only from bases inside the
  module package.
- **Reads** run in a provider read unit (or the request's shared read-scope
  connection), which is always rolled back. A read that writes, publishes,
  audits or queues an `on_commit` effect raises `ReadContractWriteError`
  instead of silently losing the write.
- **Commands** commit their own unit and must name the parameter carrying the
  idempotency key:

```python
from sandloom.sdk import command, contract

@contract("billing.credits.v1")
class Credits:
    @command(idempotency_key="operation_key", risk="money")
    async def spend(self, customer_id: UUID, amount: int, operation_key: str) -> int:
        ...
```

  `idempotency_scope=("tenant_type", "tenant_id")` partitions the keys by
  named domain arguments (changing it changes the dedupe namespace).
  `risk="money"` declares a money source at its origin (ADR-0009): it is
  recorded in the describe document and the snapshot, and a call inside a
  request makes that request a sensitive write with its automatic audit row.
  `database=` pins the provider database for an unregistered provider in
  tests.
- **Read options.** `@contract.read(planned=True, database="x")` keeps the
  read options of the L2 form: `planned=True` declares a read whose body runs
  only declared planner reads, with no unit; `database=` picks the database.
- **`db()` inside a contract method** is the provider unit's session, on the
  dedicated contract pool with its row-level-security hook. Contract methods
  take no session parameter; a caller that passes one gets
  `ContractArgumentError` (422).
- **Errors.** Raise a domain exception carrying `http_status` (or an
  `HTTPException`). The caller gets `ContractCallError` with the same status,
  detail, headers and code, locally and over RPC. An `AttributeError` /
  `ValueError` escaping provider code is a provider failure (500 locally, 503
  remotely).
- **Locks.** Never hold a row lock that a contract you call will also take:
  the call runs in another transaction on another connection, and waits at
  most `CONTRACT_DB_LOCK_TIMEOUT_MS`.

`sandloom add contract <module> <name>` writes the provider and a DTO in the
module's `schemas.py`, then runs `sandloom generate` (the stub and the DTO's
copy). See [Add a contract](add-contract.md).

### Wire types and DTOs

Generated DTO names stay unchanged when unique. If provider submodules declare
models or enums with the same class name, their copies receive distinct,
origin-derived suffixes; the generated stubs reference the matching copy.
Use the stub's annotations rather than guessing a generated DTO class name.

Every parameter and the return must be a wire type: JSON scalars, `None`,
`UUID`, `datetime` / `date`, `Decimal`, `Enum`, `Literal`, pydantic models,
`list` / `tuple` / `set` / `frozenset` / `dict` and unions of those, or `Any`.
No `*args` / `**kwargs`. Both paths bind arguments with Python's rules and
decode results against the annotations, so a local caller receives exactly
what a remote one does (a model, not a dict; an `Enum`, not its value).

**The provider authors its DTOs.** A pydantic model or `Enum` a signature
names lives in the provider's own module, by convention its `schemas` module
(`schemas.py` or the `schemas/` package):

```python
# notes/schemas.py
class NoteSummary(BaseModel):
    note_id: UUID
    title: str = Field(max_length=200)
    status: Literal["open", "done"] = "open"

# notes/contracts.py
@contract("notes.read.v1")
class NotesRead:
    async def summary(self, note_id: UUID) -> NoteSummary | None: ...
```

`sandloom generate` writes a wire-shape copy of every such DTO, nested ones
included, into the shared DTO package next to the stub
(`<app>.shared.contracts.notes_dto`, `sandloom.shared.contracts.<module>_dto`
for the standard modules), marked `@dto_copy("<module>:<class>")`; the stub
names the copy. So every caller process imports the same generated class and
needs nothing of the provider's package:

- a `Contract(REF)` caller (and `.client()`) receives the copy's class, on
  the local path and over RPC;
- the provider receives its own class: an argument is bound against the
  provider's annotations on both paths, so a copy the caller passes becomes
  the provider's DTO;
- a class already in a shared package (`sandloom.shared.*`, a
  `CONTRACT_DTO_PACKAGES` package) is referenced, not copied.

A copy carries the fields, their types and `Field` constraints, defaults,
aliases (an `alias_generator` is resolved into each field's `alias=`),
`Literal` / `Enum` values, nested models and the `model_config` options. It
does not carry methods, properties or validators: a validator is the
provider's and runs where the provider binds an argument or builds its
result.

**The closed set.** A contract DTO has the shapes an [event field](events.md#what-an-event-field-may-be)
may have, restricted to the contract wire types (no `time` / `timedelta`),
plus what a copy can write: no parametrised generic model, a default that is
a JSON scalar, an `Enum` member, a `UUID` / `Decimal` / `date` / `datetime`
or a container of those, `default_factory` among `list` / `dict` / `set` /
`frozenset` / `tuple`, constraints as `Field(...)` keywords (no
`StringConstraints` transformation), finite floats (`allow_inf_nan=False`).
`@contract` refuses anything else when it decorates the provider, naming the
field path and the fix ([SANDLOOM-C012](../../reference/error-codes.md#sandloom-c012)).
A level-2 caller (`service(name)`, `core.get_service`) gets what the runtime
returns: the provider's class locally, the copy (or plain data) remotely.

**Unions are supported.** A fully typed signature uses the faster typed codec
(codec 2) once the provider's contract hash matches. An ambiguous union such
as `UUID | str` uses the tagged codec (codec 1);
[SANDLOOM-C026](../../reference/error-codes.md#sandloom-c026), an information hint,
suggests a discriminated type so codec 2 applies. Immutable exact-type values
skip the local round trip entirely.

### Call

```python
--8<-- "examples/tutorial/tasklist/modules/tasks/api.py:quota"
```

then `if not await quota.may_create_item(): ...`.

- **The reference comes from the generated stub** (`<app>.shared.contracts.<x_y_v1>`
  or `sandloom.shared.contracts.<x_y_v1>`): the Protocol plus
  `X_Y_V1: ContractRef[Protocol] = ContractRef("x.y.v1")`. `Contract(REF)`
  is typed as the Protocol, so mypy checks every call. The stub is a
  generated file written by `sandloom generate`; `sandloom test` fails on a
  stale one ([SANDLOOM-T007](../../reference/error-codes.md#sandloom-t007)).
- **Declare it at module level.** `define_module` collects module-level
  `Contract(...)` objects into the module's `consumes` (name and `required`
  exact). The methods come from the direct `quota.<method>(` calls in that
  file; any other use (passed on, stored, `getattr`, `.client()`) means
  "every method", which checks compatibility more strictly, never less.
- **Commands need a key from business ids**, never a random one, so a retry
  replays instead of running twice:
  `await credits.spend(customer_id=..., amount=10, operation_key=operation_key("order", order_id, "charge"))`
  (`operation_key` from `sandloom.sdk.effects`).
- **A command is not atomic with the caller.** It has committed when it
  returns; if the caller later rolls back, the command stays applied. Design
  it as a saga step: compensate, or drive the effect from an event. See
  [Sagas](sagas.md).

### Required or optional

`Contract(REF)` is **required**: startup fails if the provider is neither
local nor bound in `SERVICE_BINDINGS` ([SANDLOOM-B005](../../reference/error-codes.md#sandloom-b005)).

`Contract(REF, optional=True).client()` returns the Protocol or `None`. It is
`None` only when the provider is not deployed (decided at startup); a
deployed provider that is down still raises (503). Use it only where the
caller has a correct business answer without the provider: a feature that is
off, an enrichment that can be skipped. A gate whose absence must deny is
required. For example, a quota check must never silently allow writes when
the quota service is absent.

```python
from sandloom.sdk import Contract

avatars = Contract(MEDIA_AVATARS_V1, optional=True)   # enrichment only

client = avatars.client()
url = await client.url_for(user.user_id) if client is not None else None
```

### Local equals remote

The same code runs as a monolith or split by configuration. Resolution is
local-first: a co-deployed provider wins; otherwise a generated RPC proxy from
`SERVICE_BINDINGS`; otherwise the contract is not deployed (required:
startup refusal; optional: `None`; at call time `ServiceUnavailableInDeployment`,
[SANDLOOM-R005](../../reference/error-codes.md#sandloom-r005)). Over RPC the request is
authenticated per service pair, the tenant is bound only from the MAC-covered
context, and only calls listed in `RPC_ALLOWED_CALLS` are allowed. A timeout
is `503`, and a command may have committed: retry with the same key. Keys,
transport and error mapping are in [Service RPC](../../operate/service-rpc.md).

Inside one request, local reads share one provider connection (READ
COMMITTED, rolled back, never the caller's transaction). Identical reads,
local or remote, are memoised for the request; any command clears the memo.
A worker or script that makes several local reads in a row opens the scope
itself: `async with contract_read_scope(): ...` (`sandloom.sdk.contracts`).

### Changing a contract

`sandloom generate` writes each provider's describe document to the module's
`contracts.snapshot.json` and the declared surface (`contracts`, `consumes`)
to `module.snapshot.json`. A breaking change (a removed method or parameter,
a newly required parameter, an incompatible type, a changed kind or
idempotency key) fails and nothing is written
([SANDLOOM-T004](../../reference/error-codes.md#sandloom-t004)); an additive
change is written (review the diff). `sandloom test` only checks. A breaking
change is a new contract version, `x.y.v2`, provided alongside `.v1` until no
caller uses `.v1`.

At runtime a caller compares every bound remote provider's describe document
with the snapshot shipped in the provider's package: an incompatible provider
fails `/ready` with `contract_incompatible` (`CONTRACT_COMPAT_CHECK=ready`, the
default; `strict` aborts startup; `off` checks reachability only).
`sandloom contracts check --against <snapshot|url>` reports breaking changes
between releases.

### Testing

`fake_contract(STATS_QUOTA_V1, impl)` serves the contract from `impl` for a
block. `assert_contract_conformance(core, "x.y.v1", [ContractCase(...)])`
proves that local and in-process RPC calls agree. See the
[testing reference](../../reference/testing.md).

## What breaks it

| Code | When |
| --- | --- |
| [SANDLOOM-B018](../../reference/error-codes.md#sandloom-b018) | a stateful provider (`__init__`, instance attributes), a public member that is not `async def`, `@command` without a key or naming a missing parameter, a base class outside the package |
| [SANDLOOM-B005](../../reference/error-codes.md#sandloom-b005) | a required consumed contract is neither provided locally nor bound |
| [SANDLOOM-T004](../../reference/error-codes.md#sandloom-t004) | `contracts.snapshot.json` does not match the provider (a breaking change, or a stale file in CI) |
| [SANDLOOM-T007](../../reference/error-codes.md#sandloom-t007) | the generated stub is out of date |
| [SANDLOOM-R005](../../reference/error-codes.md#sandloom-r005) | a call to a contract that is neither local nor bound |
| [SANDLOOM-C012](../../reference/error-codes.md#sandloom-c012) | a signature DTO outside the closed set, another module's or a function-local class, a missing or hand-edited generated copy |
| [SANDLOOM-C026](../../reference/error-codes.md#sandloom-c026) | information: an ambiguous union uses codec 1 |

??? note "Guarantees, precisely"
    - **Provider-owned unit (G-CONTRACT-01, 04, 15).** Every call, local or
      remote, runs in the provider's unit on the provider's database, on a
      dedicated contract pool; the caller's session is untouched.
    - **Reads (G-CONTRACT-02, 13, 14, 18).** Always rolled back; a write
      attempt raises `ReadContractWriteError`; shared read-scope connection
      and per-request memo; no idempotency record.
    - **Commands (G-CMD-01 to 09, G-CONTRACT-03).** The dedupe record
      `(contract, method, bound tenant, calling end user, key) -> (args
      digest, status, result)` is claimed in the command's own unit before
      the body runs and commits iff the effect commits, so a retry after a
      crash past COMMIT replays the result and never re-runs. The calling end
      user (the request's subject, or the signed caller facts of an RPC call) is in
      the scope because the body authorizes it: another user reusing the key
      runs the body and gets its own decision, never the stored result
      (G-BILLING-20). Same key with other arguments: 409
      `ContractIdempotencyConflict`. A concurrent duplicate waits up to
      `CONTRACT_DB_LOCK_TIMEOUT_MS`, then 503 `ContractCallInProgress`. A
      replay marks the caller's request like the first execution: the
      record keeps the call effects (risks and their audit row, the
      subject's self-revocation, pending step-ups; never a
      re-authentication or a call in doubt) and the replay re-applies them,
      locally and over RPC (G-SENS-04). The command's `on_commit` callbacks
      run after its COMMIT, in the caller's request, and their effects are
      part of it: the record commits with what the body left plus what each
      queued callback declares (a `@sensitive_effect` callback's risk and
      step-up, fail closed) and is corrected once the callbacks ran (a
      closure's effects included). If the process dies in between, the
      replay marks the declared callbacks' risks and nothing of a closure.
      A failed command leaves no record,
      so its retry runs afresh (G-CMD-04). That includes an *earlier*
      attempt still in flight: when the proxy's attempt 1 got no answer and
      its attempt 2 failed with an answer, attempt 1 may still reach the
      provider later, run the body afresh and succeed, while the caller saw
      the failure. This is the semantics of a client retry after a failure
      (no fence orders the attempts of one call); the unanswered attempt
      keeps the caller's request in doubt (its intent windows refuse,
      G-UNIT-02; so does an answer whose MAC-bound effects the caller could
      not merge: one over `RPC_MAX_RESPONSE_BYTES`, a reply without a
      well-formed envelope, an unauthenticated refusal), and a body must decide on the state it reads, never on
      an earlier attempt's outcome. A result over 256 KiB is a
      non-replayable marker (409). Records expire after
      `CONTRACT_COMMAND_DEDUPE_TTL_SECONDS`. Local and RPC calls share one
      record. `no_dup` holds iff the provider database has the
      `command_idempotency` feature and the TTL covers the caller's retry
      horizon (G-WINDOW-01); the cache fallback of a driver without it is
      fail-closed (503 `ContractIdempotencyUnavailable`) but gives **no**
      `no_dup` (not crash-safe). `no_loss` holds only while the caller
      retries with the same key until a definite answer.
    - **Not atomic with the caller.** A command commits before it returns.
    - **Local equals remote (G-CONTRACT-05 to 11, 16, 17).** One binding
      and codec path, identical results and errors.
    - **Startup and compatibility (G-CONTRACT-12, G-COMPAT-01 to 07).**
      Required contracts are present at startup; snapshots and live describe
      documents catch breaking changes.

    The mechanism matrix and the ambiguous-COMMIT table are in
    [ADR-0005](../../adr/0005-effect-model.md); the ids are in the
    [guarantee ledger](../../reference/guarantees.md). See also
    [Guarantees and labels](../guarantees/index.md).

??? note "Level 2 form"
    `@contract` and `Contract` are built from L2 primitives that stay public
    in `sandloom.sdk.contracts` and `sandloom.sdk.module`, for code that L1 does not
    cover:

    | L1 | L2 equivalent |
    | --- | --- |
    | `@contract("x.y.v1") class P` | each method as `@classmethod @contract_method(...)`, then `core.register_service("x.y.v1", P)` (`database="x"` picks the provider database) |
    | `@command(idempotency_key="k", idempotency_scope=("a",))` | `@contract_method(command=True, idempotency_key="k", idempotency_scope=("a",))` |
    | `Contract(REF)` | `ContractRequirement(REF.name, methods=(...))` in `consumes=` plus `service(REF.name)` |
    | `Contract(REF, optional=True).client()` | `ContractRequirement(..., required=False)` plus `service(REF.name, optional=True)` |

    In the L2 form `@classmethod` goes on top and `@contract_method` beneath
    it. Use it in a hand-written `register(core)` only; a module declared with
    `define_module` uses the L1 form.

## Design rationale

- [ADR-0005: the effect model](../../adr/0005-effect-model.md) (the command log
  as a dedupe record).
- [ADR-0004: RPC pair-MAC request authentication](../../adr/0004-rpc-pair-mac-request-authentication.md).
- [ADR-0010: Public API 1.0](../../adr/0010-public-api-1.0.md) §3 (typed
  contracts).
- [ADR-0009](../../adr/0009-route-policy-and-session-freshness.md) (risk declared
  at its source).
