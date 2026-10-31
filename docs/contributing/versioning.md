---
title: Versioning & Stability
---

# Versioning & Stability

Sandloom follows [Semantic Versioning](https://semver.org/). This page defines what
"public" means and the compatibility guarantees for each surface.

## What is public

The stable, supported surface is the `sandloom.sdk` namespace
([ADR-0010](../adr/0010-public-api-1.0.md)) — the only framework code a
business module and its tests may import, together with generated contract
stubs and contract DTOs:

- `sandloom.sdk` — the beginner names, re-exported from the modules below.
- `sandloom.sdk.module`, `sandloom.sdk.app`, `sandloom.sdk.migrations`, `sandloom.sdk.tooling` —
  module declaration, the app composition root, migration runners, CLI entry
  points.
- `sandloom.sdk.web`, `sandloom.sdk.data`, `sandloom.sdk.tenant` — routes, sessions and
  dependencies; `Base`, `TenantScoped`, `db()` and the read planner; tenant
  binding.
- `sandloom.sdk.events`, `sandloom.sdk.effects`, `sandloom.sdk.contracts`, `sandloom.sdk.sagas`,
  `sandloom.sdk.projections` — cross-module communication and effects.
- `sandloom.sdk.authz`, `sandloom.sdk.security`, `sandloom.sdk.infra`, `sandloom.sdk.errors` —
  authorization, token and session security, capabilities, exceptions.
- `sandloom.sdk.testing` (with `sandloom.sdk.testing.fixtures` and
  `sandloom.sdk.testing.plugin`) — the test kit.

Each module declares `__all__` and `__beginner__` (the beginner tier is a
subset); the [SDK reference](../reference/sdk/index.md) is generated from
them. `public_api_baseline.json` records every public name and
signature, and a change fails `sandloom test` (SANDLOOM-T008) until it is reviewed.

`sandloom.driver` is public for driver packages and **experimental**: it is outside
this stability promise and may change in any release. Pin a driver to one minor
range of `sandloom`. A contract joins it only with a conformance kit in
`sandloom.driver.testing`.

Everything else (`sandloom.core.*`, modules under `sandloom.sdk` whose name starts with
an underscore, `sandloom.shared.authz`, `sandloom.shared.guards`) is **internal** and
may change in any release without notice. `sandloom check` blocks imports of
internals from business modules (SANDLOOM-C002) and their tests (SANDLOOM-C013).

## Compatibility guarantees

Within a major version (`0.x` while pre-1.0, `x.y` thereafter):

- **No removal or breaking change** to a public symbol's signature without a
  major bump. Additive changes (new optional parameters, new helpers) are minor.
- **Deprecation before removal:** a public symbol is marked deprecated for at
  least one minor release (with a documented replacement) before removal in the
  next major.
- **Pre-1.0 caveat:** at `0.x`, minor versions may contain breaking changes;
  they are called out in the [Changelog](../changelog.md).

## Release line and distributions

The framework, the `sandloom` distribution, has one release line (`0.6.0`,
`0.7.0`, …), recorded in the [Changelog](../changelog.md) with an upgrade guide
per breaking release (e.g. [Upgrading to 1.0](../guides/upgrading-1.0.md)).

The module packages `sandloom-identity`, `sandloom-tenancy` and
`sandloom-billing` (see [Publishing](../guides/publishing.md)) **version
independently**, like any third-party module: each has its own version and
changelog. The current published alpha is `0.7.0a4`; each module depends on a compatible framework range, <!-- sandloom-release -->
`sandloom>=0.7.0a4,<0.8` (for a `0.x` framework the next minor is the next breaking
release). No module package depends on another. A module release that needs a
newer framework widens or moves its range; a framework minor release requires
each module package to release a version whose range admits it.

Third-party dependencies are ranges too (`>=tested,<next breaking release`);
exact versions live only in `uv.lock`. The `sandloom new` generator depends on
`sandloom[redis,celery]>=0.7.0a4,<0.8` plus `sandloom-<module>>=0.7.0a4,<0.8` for each
`--with` module, since the code it generates uses that release's API.

## Event schema evolution

Event channels are versioned independently of the package, in the channel name
(`<module>.<event>.vN`). The compatibility policy is normative and enforced at
schema registration time — see
[Event Contracts → Evolution policy](../reference/event-contracts.md#4-evolution-policy).

In short: additive, optional changes are allowed in place within `vN`; any
breaking change requires a new `vN+1` channel, and producers may dual-publish
during a migration window.

## Service contract evolution

Sync service contracts (`@contract("name.vN")`, called through
`Contract(REF)`) carry the same major version in their name. A breaking
change to a contract's method signatures is a new `vN+1` contract; the old
one is kept until consumers migrate.

## Configuration

Settings (env vars) are part of the public surface. Renaming or removing a
setting, or changing its default in a behavior-changing way, is a breaking change
and is noted in the [Changelog](../changelog.md). New settings with safe defaults
are additive.

## Database migrations

Each module owns its own migration chain (per-module-per-database). Migrations are
forward-only in production; a breaking schema change ships as an additive,
backward-compatible migration plus a follow-up cleanup, never an in-place
destructive change against live data.
