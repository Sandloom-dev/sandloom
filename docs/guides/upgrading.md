---
title: Upgrade an application
description: Review and apply Sandloom package upgrades, generated contracts and database migrations with a rollback plan.
---

# Upgrade an application

Sandloom is an alpha. Pin and review the versions you deploy; do not infer
compatibility from a passing installation alone. The current published alpha
is `0.7.0a4`. See the [changelog](../changelog.md) and <!-- sandloom-release -->
[versioning policy](../contributing/versioning.md).

1. Record the installed kernel and optional battery versions, the application
   revision and deployed topology. Back up the database and security state
   using the [runbook](../operations/runbook.md).
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
See [module maps](module-context.md) and the
[architecture explorer](architecture-graph.md) for formats and freshness.

For a breaking event payload, use [dual-publish versioning](event-versioning.md).
The [Public API 1.0 migration](upgrading-1.0.md) is a specific historical source
migration guide, not the general package-upgrade procedure.

If the upgrade fails, capture the error code, package versions and a redacted
reproduction for [support](../getting-started/help.md).
