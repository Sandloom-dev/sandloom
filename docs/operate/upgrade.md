---
title: Upgrade an application
description: Review and apply Sandloom package upgrades, generated contracts and database migrations with a rollback plan.
---

# Upgrade an application

Sandloom is an alpha. Pin and review the versions you deploy; do not infer
compatibility from a passing installation alone. The current published alpha
is `0.7.0a5`. See the [changelog](../changelog.md) and <!-- sandloom-release -->
[versioning policy](../contributing/versioning.md).

1. Record the installed kernel and optional battery versions, the application
   revision and deployed topology. Back up the database and security state
   following [Backups](operator-reference.md#backups-restore).
2. Read the release notes for every package being changed, including breaking
   changes and dependency constraints. Standard battery packages and the kernel
   have independent distributions; verify their declared compatible ranges.
3. Update the application's requirements to the selected published versions,
   then run `uv sync` and review the lockfile. A `--framework-source path` app
   consumes its local checkout; switch sources deliberately before assessing
   published-wheel behavior.
4. Run `uv run sandloom generate`, review generated changes, and run
   `uv run sandloom test --ci`. A compatibility error requires the documented
   migration or versioned contract/event rollout, not a deleted snapshot.
5. Review database migration plans in a staging copy. Run
   `uv run sandloom migrate upgrade-all` to inspect the plan and add `--apply`
   only when ready to execute it. Verify health, readiness and representative
   authenticated, tenant and billing flows.
6. Deploy using the reviewed package lock and migration plan. Keep the previous
   application artifact and a database-aware rollback/recovery plan; reverting
   Python code does not automatically undo schema or external payment effects.

## Refresh inspection tools and agent guidance

After installing a new package version, regenerate the authoring index and
refresh shipped skills from the application root:

```bash
uv run sandloom generate
uv run sandloom add skills
uv run sandloom graph
```

`generate` loads the application in its configured authoring environment and
refreshes contracts, snapshots and `.sandloom/inspection.json`. Cold `graph`
and `context MODULE` read that saved model; they never regenerate implicitly.
`add skills` preserves locally edited skills and reports conflicts for review.
See [module maps](../modules/tooling/module-context.md) and the
[architecture explorer](../modules/tooling/architecture-graph.md) for formats and freshness.

For a breaking event payload, use [dual-publish versioning](../modules/between/event-versioning.md).
The [Public API 1.0 migration](upgrade-1.0.md) is a specific historical source
migration guide, not the general package-upgrade procedure.

If the upgrade fails, capture the error code, package versions and a redacted
reproduction for [support](../getting-started/help.md).

## Upgrading from 0.7.0a4 to 0.7.0a5

This alpha changes infrastructure extension APIs. Update the framework and
standard modules together, then regenerate and run the application gates before
deployment. Ordinary built-in accessor names such as `cache()` and `notifier()`
remain available. Custom capability declarations and driver packages need the
changes below ([ADR-0011](../adr/0011-driver-api.md)):

| Before | After |
| --- | --- |
| `capability("x", name, contract=X)` | `capability(X_REF, name)`, with `X_REF: CapabilityRef[X]` published by the capability's package |
| `Capability("x", X, name)` | `Capability(X_REF, name)` |
| `CapabilityRequirement("cache", features={"incr"})` | No feature needed: `incr` is required of every driver. The same goes for `get`, `set`, `add` and `delete`, for the object store's `put`, `get`, `delete` and `url`, and for `enqueue` |
| A driver package's `register(registry)` in the `sandloom.infra` group | The class by name in the `sandloom.drivers` group, with `contract_version` and `supported_features` on the class ([Write a driver](../infrastructure/write-a-driver.md)) |
| `STORAGE_TYPE=s3` starting with a placeholder | Install a driver package that publishes `objectstore.s3` |

An experimental capability in production now needs `experimental_capability` in
`SANDLOOM_ACCEPTED_RISKS`. Test URLs with `localhost` and a port from 32768 up are refused:
use `127.0.0.1`.
