---
title: Reference
description: Lookup material, generated from the code wherever possible - the sandloom.sdk Python API, the error catalogue, testing, CLI, configuration, routes and guarantees.
type: reference
---

# Reference

Lookup material, generated from the code wherever possible. To learn by
doing, start with the [tutorial](../getting-started/tutorial.md); to
understand why, start with [What a module is](../concepts/modules.md).

| Reference | Contents |
| --- | --- |
| [Python API (`sandloom.sdk`)](sdk/index.md) | every public module and name with its exact signature, beginner names first (generated) |
| [Driver kit (`sandloom.driverkit`)](driverkit.md) | the API for driver packages; modules never import it |
| [Error catalogue](error-codes.md) | every `SANDLOOM-*` code: what happened, the fix, its status (generated; also `sandloom explain <code>`) |
| [Testing](testing.md) | the `sandloom.sdk.testing` kit, the fixtures and the harness rules (copy-paste) |
| [CLI](cli.md) | every `sandloom` command |
| [Operator reference](../operations/operator-reference.md) | incident tools, flags, dry runs and production confirmation |
| [Configuration](configuration.md) | every environment variable, generated from the settings model |
| [Configuration tiers](configuration-tiers.md) | which settings change a guarantee, per profile (generated) |
| [Routes](routes.md) | every platform route and its resolved policy (generated) |
| [Event contracts](event-contracts.md) | channel naming, the schema registry, validation, evolution |
| [Security model](security-model.md) | authentication, tenant isolation, idempotency, audit, secrets, the production profile |
| [Guarantees & capabilities](guarantees.md) | the ledger: every guarantee with the tests that enforce it, and the recorded gaps (generated) |
| [Execution planner](execution-planner.md) | declared read operations and the planner |
| [SLOs](slo.md) | SLIs from the shipped metrics, example SLOs and an error-budget template |
| [Dependency stack](dependencies.md) | the libraries Sandloom builds on |
| [Standard modules](../modules/index.md) | identity, tenancy and billing: routes, events, contracts |

Regenerate the committed generated pages (`sdk/` and the error catalogue)
with `uv run python tools/docs/docs_reference.py`; the docs
build fails while they are stale.
