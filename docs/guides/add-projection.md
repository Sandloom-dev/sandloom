---
title: Add a projection
description: Build a read model fed by events with `sandloom add projection` - full-state, delta and tombstone mappers, source groups, reads through View, and rebuilds.
type: how-to
---

# Add a projection

**Goal:** a table your module owns and keeps up to date from events, such as
"boards with their title" or "members with their plan", so a list endpoint
reads one table instead of joining other modules' data at request time.

**Use it** when an endpoint lists or filters data that other modules (or
other aggregates of your module) own, and eventual consistency is
acceptable.

**Do not use it** to check a strong invariant: a projection lags its
sources, so call the owner's command contract and let it refuse. Do not use
it for a single counter fed by one channel without versions; a plain
[consumer](add-consumer.md) is enough. Background:
[Projections](../concepts/projections.md).

## Prerequisites

- The module exists and keeps its tables in a `models/` package, as
  `sandloom add module` lays it out (the generator refuses otherwise).
- The source events carry the aggregate id, `tenant_id` and a per-aggregate
  `version` that the producer increments on every change. Without a version
  a projection cannot order events, because delivery has no ordering
  guarantee.

## Steps

### 1. Run the generator

```bash
sandloom add projection notes board
```

It writes three new files in the module package:

- `models/board_row.py`: the row model;
- `projections.py`: two example source events and the projection;
- `migrations/versions/notes_board_projection.py`: the migration that
  creates the table, chained after the module chain's current head (the
  generator refuses a chain with several heads).

### 2. Read what it wrote

The row: one row per aggregate, `TenantScoped`, with the owned columns, the
group's version column and the tombstone column:

```python
--8<-- "docs/snippets/generated/projection/demo/modules/notes/models/board_row.py"
```

The projection:

```python
--8<-- "docs/snippets/generated/projection/demo/modules/notes/projections.py"
```

- **`key`** identifies a row and must start with `tenant_id`.
- **A `SourceGroup`** is the set of channels of **one** aggregate (here
  saved and deleted) that share one version domain: `aggregate_key` and
  `version_field` name fields of every event in the group, `version_column`
  is the column that stores the version, and `owns` lists the columns this
  group writes. Every column is owned by exactly one group or seeded with
  `insert_only`.
- **Mappers** turn an event into the owned fields:
  - `full_state(fn)`: the event carries every owned field; the row is
    replaced only if the stored version is NULL or older. This converges for
    any interleaving and any number of duplicates.
  - `tombstone(fn)`: a delete, stored as a versioned tombstone
    (`deleted_at`); reads never return it, and an older event cannot
    resurrect the row. A later full-state event clears it.
  - `delta(fn)`: an increment (`{"count": 1}`), added to the stored value.
    Deltas are not convergent under "skip older", so they are allowed only
    in the consumer's own database under the durable inbox, which applies
    each event exactly once (not with `at_least_once=True`).
- **Discovery** registers one durable consumer per source channel, named
  `<projection>@<channel>`, binding the tenant from the event's `tenant_id`.
  The projection's consumers apply each event in the consumer's unit of
  work.

The migration, with row-level security for the tenant table:

```python
--8<-- "docs/snippets/generated/projection/demo/modules/notes/migrations/versions/notes_board_projection.py"
```

### 3. Point it at your real sources

The generated events are the module's own channels (`notes.board.saved.v1`),
so they are producers defined next to the projection. In a real projection:

1. **Your own aggregate:** move the event classes to `events.py`, import
   them in `projections.py`, and publish them from your services with the
   aggregate's version.
2. **Another module's aggregate:** declare a *view* of its channel (an
   `Event` subclass with the fields you need, as
   [`sandloom add consumer`](add-consumer.md) writes it) and use it in the
   group. Never add a field the producer does not send.
3. **Several aggregates in one row** (a member and their plan): one
   `SourceGroup` per aggregate, each with its own version column and its own
   owned columns.
4. Change the model, then write a new revision with
   [`sandloom migrate new`](write-migration.md); edit the generated migration
   only while it has never been applied anywhere.

### 4. Read it

Inject a `ProjectionView` with `View(name, row_type)`. Reads are filtered
to the bound tenant and never return tombstoned rows:

```python
from typing import Annotated
from sandloom.sdk.data import Page
from sandloom.sdk.projections import ProjectionView, View

@router.get("/boards")
async def list_boards(
    boards: Annotated[ProjectionView[BoardOut], View("notes.board", BoardOut)],
    cursor: str | None = None,
) -> Page[BoardOut]:
    return await boards.query(limit=50, cursor=cursor)
```

`get(key)` returns one row or `None`; `query(filter=, limit=, cursor=)`
returns a page ordered by the key.

### 5. Make it rebuildable

A projection is a cache: rebuilding it from the sources is the repair path
after corruption or a schema change. Pass `rebuild_from=` a read method of
the source's contract, through its caller:

```python
boards = Contract(NOTES_BOARDS_V1)

BOARD = Projection(..., rebuild_from=boards.list_for_tenant)
```

`list_for_tenant(tenant_id)` returns the aggregates' current states, as
instances of the group's events or as mappings of its single full-state
event. The rebuilder applies them through the same version-guarded path as
the consumers and never infers a delete from a listing. Run it as a one-off
job once the source services are up, never in the `migrate` one-shot:

```bash
sandloom projection list
sandloom projection rebuild notes.board --tenant <tenant id>
sandloom projection rebuild notes.board            # every tenant the projection holds
```

## Purge tombstones

Tombstones must outlive every replay. Purge them only through the framework,
never by hand:

```bash
sandloom projection purge notes.board --older-than-days 30
```

The purge refuses an age below the replay horizon (stream retention plus the
relay's redelivery horizon, the oldest DLQ entry and the oldest dead outbox
row of the source channels) and while any rebuild of the projection runs
(each running rebuild is counted: one finishing never lifts the exclusion
of another). It raises the
projection's purge watermark before deleting, and from then on an event
emitted before the watermark is dead-lettered instead of resurrecting a row.

## Read your own writes

Set `PROJECTION_TOKEN_KEYS` (`{kid: secret}`). A write route whose events
feed a projection then answers with `SANDLOOM-Consistency-Token`; the client
sends it back on the read, and `View(...)` waits up to `wait_ms` for those
rows before serving (stale reads carry `SANDLOOM-Projection-Stale: true`).

`ProjectionView.search()` needs a store with the `search` feature; none ships
in 1.0, so it raises `NotImplementedError`.

## Verify

Deliver source events in a test and read the projection back: deliver an
older version after a newer one and check the newer fields win; deliver a
tombstone and check the row disappears from `query()`; deliver the same
event twice (`deliver(event, times=2)`).

```bash
sandloom test notes
```

## Errors you may hit

- [SANDLOOM-B012](../reference/error-codes.md#sandloom-b012): the key does not start
  with `tenant_id`; a column is owned by two groups, or is both owned and
  `insert_only`; two groups share a version column; a mapper is not
  `full_state`, `delta` or `tombstone`; a delta runs without the inbox; a
  tombstone has no `deleted_at` column; an event lacks the group's
  `aggregate_key` or `version_field`; the model lacks a declared column.
- [SANDLOOM-B006](../reference/error-codes.md#sandloom-b006): the projection is
  defined outside the module's `projections` submodule.
- [SANDLOOM-B015](../reference/error-codes.md#sandloom-b015): a source event class is
  invalid.
- [SANDLOOM-T002](../reference/error-codes.md#sandloom-t002): the row table is not
  created by the module's migration chain.

## Related

- [Projections](../concepts/projections.md): the convergence model.
- [Add a consumer](add-consumer.md): views of other modules' channels.
- [Events and consumers](../concepts/events.md): why there is no ordering.
- [Write a migration](write-migration.md).
