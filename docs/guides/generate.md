---
title: Generate contracts and snapshots
description: When to run sandloom generate, what it writes, how to check for drift and how to handle a rejected compatibility change.
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

| File | Purpose |
| --- | --- |
| A module's `module.snapshot.json` | Declared events, consumers, contracts, sagas and projections |
| `routes.snapshot.json` and `contracts.snapshot.json` | Route policy and contract compatibility baselines |
| Shared `contracts/*.py` | Typed contract references, protocols and copied DTOs |
| Shared `events/*.json` | Producer wire schemas used to check consumers |

These files are reviewed and committed with the module change. Edit the source
schema, provider or declaration, then regenerate; generated files are not an
independent source of truth. The exact shared-package location is configured
by the application's layout and DTO packages.

To check without writing:

```bash
uv run sandloom generate --check
uv run sandloom test --ci
```

The CI gate also checks generated files and never repairs them. A stale file
fails with the regeneration command. To work on selected modules, pass their
names, for example `uv run sandloom generate notes`.

## A compatibility failure

Generation validates compatibility before writing. A breaking event or
contract change needs a new version, coordinated consumers and a rollout;
repeating the command or deleting a baseline is not a rollout strategy.
Read [event versioning](event-versioning.md),
[contracts](../concepts/contracts.md) and the named error's explanation.

A new, unpublished tutorial contract should be authored in its intended final
form before its first generation. The [tutorial](../getting-started/tutorial.md)
does this explicitly. Database changes are separate: write a revision with
`sandloom migrate new`, then apply it with `sandloom migrate upgrade-all --apply`.
