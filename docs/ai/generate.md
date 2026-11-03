---
title: Generate contracts and snapshots
description: When to run sandloom generate, what it writes, how to check for drift and how to handle a rejected compatibility change.
type: how-to
---

# Generate contracts and snapshots

Run generation after installing a fresh app's dependencies and after editing a
module's declarations. It reads the application composition and writes files;
it does not apply database migrations. See the [quickstart](../getting-started/index.md).

```bash
uv sync
uv run sandloom generate
uv run sandloom test
```

A CLI launched through `uvx --from sandloom` may not have the optional batteries
installed. `uv sync` installs them into the application; `uv run sandloom generate`
then uses those actual installed versions. Follow these steps even when the
standalone CLI wrote some files during `sandloom new`.

## What is generated

Each module of your project gets its snapshots next to its code. The shared
package gets the files callers import, for every module the application
composes, installed module packages included; an installed package ships its
own snapshots.

| File | What it holds |
| --- | --- |
| `<module>/module.snapshot.json` | the module's declared surface: producer event schemas, consumers, consumed contracts, contracts, sagas and projections |
| `<module>/routes.snapshot.json` | every route and its resolved policy ([the route snapshot](../modules/http/route-policy.md#the-route-snapshot)) |
| `<module>/contracts.snapshot.json` | the describe document of every contract the module provides |
| `<shared>/contracts/<x_y_vN>.py` | the typed stub of each provided contract: its protocol and its contract reference |
| `<shared>/contracts/<module>_dto.py` | wire-shape copies of the provider's DTOs that its contract signatures name |
| `<shared>/events/<channel>.json` | the JSON schema of each produced event, so views in other repositories can be verified |

These files are reviewed and committed with the module change. They are the
one stated exception to "a module edits nothing outside its package". Edit the
source schema, provider or declaration, then regenerate; generated files are
not an independent source of truth. `sandloom add` runs generation as its last
step. The exact shared-package location is configured by the application's
layout and DTO packages.

To check without writing:

```bash
uv run sandloom generate --check
uv run sandloom test --ci
```

The CI gate also checks generated files and never repairs them. A stale file
fails with "generated files are stale: run `sandloom generate`". To work on
selected modules, pass their names, for example `uv run sandloom generate notes`.

## A compatibility failure

Generation compares every file with the committed version before writing. An
incompatible change fails and writes nothing. Incompatible changes include a
removed contract method and an event field changed on the same `.vN`. A route
that lost its authentication and a DTO the generator cannot copy fail too. A weaker route policy is approved only by
`sandloom routes snapshot --write`.

A breaking event or contract change needs a new version, coordinated consumers
and a rollout. Repeating the command or deleting a baseline is not a rollout
strategy. Read [event versioning](../modules/between/event-versioning.md),
[contracts](../modules/between/contracts.md) and the named error's explanation.

A new, unpublished tutorial contract should be authored in its intended final
form before its first generation. The [tutorial](../getting-started/tutorial.md)
does this explicitly. Database changes are separate: write a revision with
`sandloom migrate new`, then apply it with `sandloom migrate upgrade-all --apply`.
