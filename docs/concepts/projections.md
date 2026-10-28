---
title: Projections
description: Cross-module read models built from events - source groups, version guards, field ownership, tombstones and View.
type: concept
---

# Projections

A **projection** is a denormalised read model that one module owns and builds
from other modules' events. Use one when a list spans modules, such as "tasks
with their project's status": a request-time join across modules' tables, or
a contract call per row, is not allowed. The join moves to write time. A
`Projection(...)` declares the target table, the events that feed it grouped
by aggregate (`SourceGroup`), and how each event maps to the columns it owns.
Sandloom turns that into durable consumers that apply every event with a version
guard, so duplicate and out-of-order delivery converge to the same row. A
projection is eventually consistent and rebuildable: a cache, never a source
of truth.

## What you get by default / what you declare

| You get by default | You declare |
| --- | --- |
| One durable consumer per source channel, namespaced by the module, applying events in the consumer's unit | `Projection("<module>.<name>", key=("tenant_id", ...), model=..., groups=[...])` in `projections.py` |
| A version guard: a row changes only if the stored version is `NULL` or older | per group: `aggregate_key`, `version_field`, `version_column` |
| Refusal at startup when two groups write one column, a key is not tenant-first, or a delta is unsafe | per group: `owns=(...)`, and each event's mapper (`full_state`, `delta`, `tombstone`) |
| The tenant bound from the event's `tenant_id` and checked against the row | a `tenant_id: UUID` field on every source event |
| Tombstones that reads filter out | a nullable `deleted_at` column (or `deleted_column=`) when a group has `tombstone` events |
| A tenant-scoped, paginated read dependency that hides tombstones | `Annotated[ProjectionView[Row], View("<name>", Row)]` |
| A rebuild path through the source's read contract | `rebuild_from=<caller>.<read method>` |

## How to use it

### The model: one version domain per source group

1. **A source group is one aggregate.** The created, updated and deleted
   channels of one aggregate (a task, a project) share one group and one
   version column. Versions are per `(tenant_id, aggregate id, group)` and
   strictly increasing at the producer, so each event must carry its
   aggregate's version.
2. **Every column has exactly one owner.** A column is owned by one group or
   seeded once by `insert_only=` (written on INSERT, never on UPDATE). Two
   groups writing one column is refused
   ([SANDLOOM-B012](../reference/error-codes.md#sandloom-b012)). A mapper may return only
   the columns its group owns.
3. **Full-state events converge.** A `full_state` mapper returns every owned
   field; the apply is "replace iff the stored version is `NULL` or older". A
   `NULL` version (a column added later, or a row seeded by another group)
   counts as the oldest, so the first event of a group always applies. For
   any interleaving with duplicates, the row ends with each group's
   max-version fields.
4. **Deltas are the exception.** An increment is not convergent under "skip
   older". A `delta` mapper is allowed only for a relational projection in
   the consumer's own database, applied in the consumer unit under the
   durable inbox claim: exactly once by the inbox, not by the version guard.
   With `at_least_once=True` (no inbox) a delta is refused (SANDLOOM-B012).
5. **Deletes are versioned tombstones** in the owning group. A tombstone sets
   `deleted_at` under the same version guard; reads filter it; a later
   full-state event of a newer version clears it. Tombstones must outlive the
   whole replay horizon (below).
6. **A denormalised field from another aggregate** must not be "read over
   RPC, then inserted": a source event can commit in between and its update
   matches no row yet. Keep a local, version-guarded snapshot of the source,
   upsert it in the source's consumer (which locks the row) and
   `SELECT ... FOR UPDATE` it in the other writer before copying its values.
   The projecthub example's taskboard `ProjectSnapshot` is the worked case
   (`examples/projecthub`).

### Declare

```python
from uuid import UUID

from sandloom.sdk import Event
from sandloom.sdk.projections import Projection, SourceGroup, full_state, tombstone

from .models.board_row import BoardRow


class BoardSaved(Event, channel="notes.board.saved.v1"):      # a producer, or a view
    board_id: UUID
    tenant_id: UUID
    title: str
    version: int


class BoardDeleted(Event, channel="notes.board.deleted.v1"):
    board_id: UUID
    tenant_id: UUID
    version: int


BOARD = Projection(
    "notes.board",
    key=("tenant_id", "board_id"),
    model=BoardRow,                      # a TenantScoped model with the columns below
    groups=[
        SourceGroup(
            events={
                BoardSaved: full_state(lambda e: {"title": e.title}),
                BoardDeleted: tombstone(lambda e: {}),
            },
            aggregate_key="board_id",
            version_field="version",
            version_column="version",
            owns=("title",),
        )
    ],
)
```

- The source events are typed classes: producers of your own module, or
  [views](events.md#producers-and-views) of other modules' channels. Other
  modules are never imported.
- The model is `TenantScoped` and has the key columns, every owned column,
  each group's version column and, with tombstones, `deleted_at`. Ship its
  table with row-level security, as `sandloom migrate new` renders it.
- A projection fed by two aggregates has two groups, each with its own
  version column and its own owned columns.
- Projection consumers are required by default, so they gate readiness like
  any consumer.
- **A source that names its tenant differently** (the platform's member
  events carry `organization_id`): `SourceGroup(..., tenant_from="organization_id")`.
  The consumer binds that tenant and the row's `tenant_id` comes from it.
- **The source's version must be required.** An event whose version field
  is optional cannot be ordered against the stored row, and the projection
  refuses it (SANDLOOM-B012). For the organization's members, project
  `tenancy.member_added.v2` / `member_role_changed.v2` / `member_removed.v2`
  (views of them in your module): they carry `tenant_id` and a required
  per-membership `version`. A source with no version at all needs a
  hand-written `@on` consumer that re-reads the owner's state through its
  contract (the v1 member channels: `member_event_action` from
  `sandloom.shared.membership`), and `rebuild_from=` for a rebuild.

`sandloom add projection <module> <name>` writes the projection, its model and the
migration. See [Add a projection](../guides/add-projection.md).

### Read

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
returns a page ordered by the key. Tombstoned rows are never returned, and
the read is filtered to the bound tenant.

**Never validate a strong-consistency invariant against a projection.** It
lags its sources. Call the owner's command contract and let it refuse.

### Rebuild

`rebuild_from=` names the source's read method through a caller, for example
`rebuild_from=boards.list_for_tenant` with `boards = Contract(NOTES_BOARDS_V1)`.
The rebuilder reads source state through that read contract, never the
source's tables, and applies it through the same version-guarded path as the
consumers; it never infers a delete from a listing. Run a rebuild as a
one-off job after the source services are up, never in the `migrate`
one-shot:

```bash
sandloom projection rebuild notes.board --tenant <id>
```

The command runs the app's startup tasks only: no consumer runtime (it never
joins the projection's live groups) and no outbox relay. `sandloom projection
list` shows the registered projections.

## What breaks it

| Code | When |
| --- | --- |
| [SANDLOOM-B012](../reference/error-codes.md#sandloom-b012) | a key that does not start with `tenant_id`; a column owned by two groups, or both owned and `insert_only`; two groups sharing a version column; a mapper that is not `full_state` / `delta` / `tombstone`; a delta without the inbox; a tombstone without its `deleted_at` column; an event lacking the group's `aggregate_key` or `version_field`; a model missing a declared column |
| [SANDLOOM-B016](../reference/error-codes.md#sandloom-b016) | a source consumer that cannot be registered (as for any consumer) |
| [SANDLOOM-C025](../reference/error-codes.md#sandloom-c025) | a source view incompatible with its producer |
| `TenantMismatchError` | a row whose `tenant_id` differs from the bound tenant |

??? note "Guarantees, precisely"
    - **Convergence (C-PROJ-01, C-PROJ-04).** A full-state apply changes a
      row only when the stored version is `NULL` or older, so at-least-once,
      out-of-order delivery never rolls a row back; `insert_only` columns are
      written on INSERT only.
    - **Tenancy (C-PROJ-02).** Every row carries `tenant_id`; the apply
      requires it, refuses one that differs from the bound tenant, and never
      overwrites another tenant's row on a key collision.
    - **Registration (C-PROJ-05).** Each source channel gets a durable
      consumer namespaced by the module; operating commands are C-PROJ-03.
    - **Denormalised fields (C-PROJ-06).** Correct only with the locked local
      snapshot pattern.
    - **Labels.** A projection effect in the consumer's own database with the
      inbox is exactly-once effect; a full-state apply in another store is
      `no_dup` only through a conditional write. Visibility is eventual.
    - **Tombstone horizon.** Replays are not bounded by stream retention
      (`sandloom outbox requeue` and `sandloom streams dlq-replay` can redeliver old
      events), so tombstones are kept until a purge that records a monotonic
      watermark; after a purge the apply path dead-letters any event older
      than the watermark, so a replay can never resurrect a deleted row.

    See [ADR-0005](../adr/0005-effect-model.md), the
    [guarantee ledger](../reference/guarantees.md) and
    [Guarantees and labels](guarantees.md).

??? note "How it works"
    `Projection(...)` is the L1 form of a `QueryProjectionDescriptor`, one
    `ProjectionConsumerDescriptor` per source channel whose handler applies
    `monotonic_upsert` (full state) or `monotonic_delete` (tombstone), and a
    `ProjectionRebuilder` when `rebuild_from` is given. These L2 names stay
    public in `sandloom.sdk.projections`, with `monotonic_update` for a match-based
    fan-out. The helpers issue Core statements, so they enforce tenancy
    themselves: `monotonic_upsert` requires the row's `tenant_id`, and
    `monotonic_update` is scoped to the bound tenant when one is bound and
    cannot change `tenant_id`.

!!! info "Targets, tokens, rebuilds and purges"
    - **Targets.** A relational `model=` is the supported target in 1.0:
      its convergence, rebuilds and purges are guaranteed.
      `store=("document_store", "<instance>")` (a store whose driver
      advertises `conditional_write`; the in-memory `memory` driver in 1.0)
      is **experimental in 1.0**: it works and is tested, but production
      drivers and their guarantees arrive with the infrastructure wave.
      A store target applies through `put_if_newer` / `tombstone_if_newer`;
      delta mappers are refused there (SANDLOOM-B012).
    - **Read-your-writes.** A write whose events feed a projection answers
      with `SANDLOOM-Consistency-Token` when `PROJECTION_TOKEN_KEYS` is set. A
      read presenting it waits up to `wait_ms` (default
      `PROJECTION_RYW_WAIT_MS`) for those rows, then serves anyway with
      `SANDLOOM-Projection-Stale: true` (`on_stale="serve"`) or reads the entity
      from `rebuild_from` (`on_stale="source"`). Never an error status.
    - **Rebuilds.** `rebuild(name, tenant_id=...)` and `sandloom projection
      rebuild` mark the rebuild in the projection's watermark row; a
      rebuild never lowers the purge watermark. No 1.0 store swaps an
      alias, so `blue_green=True` rebuilds in place (convergent: every
      write is version-guarded).
    - **Tombstones** are purged only by `purge_tombstones(name,
      older_than=)` / `sandloom projection purge`, which refuses below the replay
      horizon and during a rebuild, and raises the purge watermark first.
      An event emitted before the watermark is dead-lettered.
    - `ProjectionView.search` needs a store with the `search` feature; none
      ships in 1.0.

## Design rationale

- [ADR-0010: Public API 1.0](../adr/0010-public-api-1.0.md) §6 (guarantees
  as a lattice, labels derived from mechanisms).
- [ADR-0005: the effect model](../adr/0005-effect-model.md) (the inbox as a
  dedupe record).
