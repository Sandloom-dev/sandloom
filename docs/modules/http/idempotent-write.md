---
title: Make a write idempotent
description: What the automatic guard already does for a write, when to opt in to body replay with replay_safe, when to opt out, how to configure an explicit idempotency_guard, and what money writes need.
type: how-to
---

# Make a write idempotent

**Goal:** a client that retries a `POST`, `PUT`, `PATCH` or `DELETE` (after
a timeout, a lost connection, a double click) never applies the effect
twice, and gets a useful answer on the retry.

**Use it** to decide what a write route needs beyond the default. In most
cases the answer is nothing.

**Do not use it** for consumers, tasks or saga steps: they dedupe at the
effect level (the durable inbox, unique keys, the command log). See
[Route policy](route-policy.md) for how the defaults are
derived.

## Prerequisites

- The route is on a `module_router()` (or another authenticated router).
- Clients send an `Idempotency-Key` header, unique per logical operation,
  and reuse it on every retry of that operation.

## What you get with no declaration

Every authenticated user write that receives an `Idempotency-Key` gets the
**automatic guard**, in **mode S** (status replay):

- The first request runs. A durable record is claimed in the route's
  request unit of work and completed just before its `COMMIT`, so it
  commits if and only if the effect commits.
- A retry with the same key after success gets the **stored status, an
  empty body** and `Idempotent-Replay: true`. The handler does not run.
- A retry while the first request is still running gets `409`.
- The same key with a different request (method, path, query, caller,
  tenant or body) gets `422`.
- A request that failed or rolled back leaves no record: the retry runs
  afresh. A `5xx` is never replayed.
- The replay is decided only after every route-level dependency: the live
  session check, the tenant binding, authorization gates and step-up. A
  caller who lost access gets `401` / `403`, not a replay.
- Keys are scoped to the route, the method, the caller and the tenant, and
  kept 24 hours.

A write without the header simply runs (money writes excepted, see below).
A route without a request unit gets the fail-closed in-flight form: a
concurrent duplicate is `409`, a cache outage `503`, nothing is stored.

Status replay never returns a body, so it cannot leak data the caller is no
longer allowed to see. The client knows the operation succeeded and reads
the resource with a normal `GET`, which runs authorization again.

## Steps

### 1. Opt in to body replay, only when it is safe

`replay_safe(reason=)` switches the automatic guard to **mode B**: a retry
gets the stored status, body and headers.

```python
from sandloom.sdk.web import replay_safe

@router.post(
    "/items",
    status_code=201,
    dependencies=[replay_safe(reason="echoes the caller's own fields and the new id")],
)
async def create_item(body: ItemIn, user: CurrentUser) -> ItemOut: ...
```

Add it **only** when the response contains nothing whose visibility could
change between the two attempts. The typical case is a creation that echoes
back the caller's own submitted fields and the new id.

**Never** add it to a response that contains:

- a secret, or a bearer capability (a URL or a token that grants access);
- data of other users;
- fields whose visibility depends on a role, an ownership or a membership
  that can change (a body stored for an owner would be replayed after an
  ownership transfer).

When in doubt, leave it out. The reason is recorded in the route snapshot
and reviewed with your change. On a sensitive write (`money`, `admin`,
`security`) it has no effect: those stay in mode S.

### 2. Opt out, with a reason

`no_idempotency(reason=)` removes the automatic guard from a write:

```python
from sandloom.sdk.web import no_idempotency

@router.post("/events/stream", dependencies=[no_idempotency(reason="SSE stream, no stored outcome")])
```

Use it for streaming responses and for writes that are naturally
idempotent by their own key (an upsert on a client-chosen id). The reason
is recorded in the route snapshot. Removing a protection from a committed
route is a weaker policy: `sandloom generate` and `sandloom test` report it
(SANDLOOM-T001) until you review it and approve it with
`sandloom routes snapshot --write`.

### 3. Configure the guard explicitly, when the default does not fit

An explicit `idempotency_guard` replaces the automatic one and keeps all its
options:

```python
from fastapi import Depends
from sandloom.sdk.web import idempotency_guard

guard = idempotency_guard(scope="notes.import", ttl_seconds=3600, required=True, durable=True)

@router.post("/imports", dependencies=[Depends(guard)])
```

| Parameter | Default | Meaning |
| --- | --- | --- |
| `scope` | required | the key namespace, usually `<module>.<operation>` |
| `ttl_seconds` | `86400` | how long a key is kept |
| `required` | `True` | a request without `Idempotency-Key` gets `400` |
| `fail_closed` | `False` | a cache outage answers `503` instead of letting the request through |
| `store_response` | `True` | store and replay the response; `False` keeps only an in-flight reservation |
| `durable` | `False` | claim the record in the request's unit of work, committed with the effect |

The combinations give the modes:

- **Mode B, durable** (`durable=True`): the record commits with the effect,
  so even an ambiguous `COMMIT` (the connection dropped while committing)
  never runs the effect twice; a retry replays the stored response.
- **Cached replay** (`durable=False`, `store_response=True`): the record is
  written to the cache after the commit. It is replay-only: a crash between
  the commit and the record lets a retry run the effect again. Use it only
  for effects that are harmless to repeat.
- **Mode X, release and re-execute** (`store_response=False`, with
  `fail_closed=True`): only an in-flight reservation, released when the
  request completes. A retry runs again: every dependency, authorization
  and the handler. The effect's own dedupe record (an intent key, the
  command log, a ledger key) keeps it single. This is the required form for
  routes that issue tokens or bearer URLs, which must never be replayed
  from storage (SANDLOOM-B019).

Declare the guard after authentication in the endpoint's dependencies; on
a `module_router()` authentication is already a route dependency.

### 4. Money writes need a dedupe record of their own

A write that moves money is a sensitive write with risk `money`. The risk
comes from its source: a `@command(..., risk="money")` it calls, a row
inserted into a `MoneyLedger` model, an effect decorated
`@sensitive_effect("money")`, or `sensitive("money")` on a route whose
authority check is hand-rolled. The request's idempotency key alone is not
enough, because the effect itself must be single:

- the effect has its **own dedupe record** in the same unit: a ledger row
  whose unique operation key is the request's key
  (`request_operation_key()` from `sandloom.sdk.web`) or a key derived from
  business ids (`operation_key(...)` from `sandloom.sdk.effects`), an intent
  key, or the provider's command log;
- a request with no `Idempotency-Key` and no dedupe record of its own gets
  `428 Precondition Required` (SANDLOOM-R010): the client must send a key;
- a route that receives a key but cannot dedupe the effect with it (for
  example it opted out with `no_idempotency` and the effect has no key of
  its own) is a developer error (SANDLOOM-R009): nothing commits;
- an explicit guard on a money route must be durable (`durable=True`) or
  mode X; a replay-only or fail-open guard is refused at startup
  (SANDLOOM-B019).

Every sensitive write also gets one automatic audit row.

## Verify

Send the same request twice with one key and check the second answer:

```python
headers = {**auth, "Idempotency-Key": "k1"}
first = await real_uow_client.post("/api/v1/notes/items", json={"title": "x"}, headers=headers)
again = await real_uow_client.post("/api/v1/notes/items", json={"title": "x"}, headers=headers)
assert first.status_code == again.status_code == 201
assert again.headers["Idempotent-Replay"] == "true"
assert len(published(ItemCreated)) == 1
```

`real_uow_client` commits for real, so the durable record behaves as in
production; `client` shares a session that is rolled back after the test.
Then:

```bash
sandloom test notes
```

The route snapshot records each route's idempotency mode and any
`replay_safe` / `no_idempotency` reason; review its diff.

## Errors you may hit

- [SANDLOOM-B002](../../reference/error-codes.md#sandloom-b002): an explicit guard
  resolves before the route's authentication.
- [SANDLOOM-B019](../../reference/error-codes.md#sandloom-b019): a token-issuing,
  operation-intent or money route has the wrong guard.
- [SANDLOOM-R009](../../reference/error-codes.md#sandloom-r009): a money effect ran
  without a dedupe record.
- [SANDLOOM-R010](../../reference/error-codes.md#sandloom-r010): `428`, the client must
  send `Idempotency-Key`.
- [SANDLOOM-T001](../../reference/error-codes.md#sandloom-t001): the route snapshot
  changed, for example a protection was removed.

## Related

- [Route policy](route-policy.md): the defaults, including the
  idempotency section.
- [Transaction and replay lifecycle](../guarantees/lifecycle.md): every route
  kind and its idempotency mode.
- [Units of Work and `db()`](../database/units.md): why the durable record
  commits with the effect.
- [Contracts](../between/contracts.md): command dedupe.
