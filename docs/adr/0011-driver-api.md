---
title: "ADR-0011: The driver API"
---

# ADR-0011: The driver API

- **Status:** Accepted (2026-10-31, by the owner)
- **Date:** 2026-10-31
- **Amends:** [ADR-0001](0001-framework-distribution.md) (how driver packages register)
  and [ADR-0010](0010-public-api-1.0.md) (a second public surface, not for modules)
- **Research:** `design/driver-api/research.md`

## Context

A contract has two readers. A module asks what it can call; a driver asks what it must
implement. They need different promises: a new abstract method does not touch a caller
and breaks an implementer. Sandloom showed both readers the same class and gave neither
a public home. Accessors returned `Any`, and a driver imported from `sandloom.core`.

Discovery had its own faults. Every installed package ran `register(registry)` at
startup in an order Python does not define, so a driver could lose to the package that
declares its capability. A failure was only logged, and a package could silently
replace a built-in driver, `sqlalchemy` included. Features were declared twice, so a
requirement could pass at startup and fail on first use.

## Decision

1. **Two doors, one class.** Modules get a Protocol of operations from
   `sandloom.sdk.infra` (`Cache`, `Notifier`, `ObjectStore`, `TaskQueue`), and accessors
   return it. Driver packages get the provider ABC, which subclasses that Protocol and
   adds the lifecycle, from `sandloom.driverkit`. A module may not import `driverkit`
   (C004) and the kernel never does (`lint-imports`).
2. **Find drivers by name, lazily.** An entry point
   `"<capability>.<driver>" = "pkg.driver:Class"` in `sandloom.drivers`, and
   `"<capability>" = "pkg.contracts:SPEC"` in `sandloom.capabilities`. Only what `INFRA`
   selects is imported. There is no `register` callable, so there is no order.
3. **Conflicts and failures stop startup.** A selected name with two providers, or a
   package claiming a built-in's name, is SANDLOOM-B021. A selected driver that cannot
   be used is SANDLOOM-B022. There is no override: another behaviour is another name.
4. **Declare once, on the class.** `supported_features`, or `features_for(options)` when
   a feature depends on an option. An instance that reports otherwise is refused. A
   required method is not a feature, and a test holds every spec to that.
5. **Required contract version.** `contract_version` on the driver class; a missing one,
   or another major version, is SANDLOOM-B022. Signatures are pinned per version by a
   test, and `sandloom.driverkit` is in the API baseline (T008).
6. **A contract is public only with a kit.** `NotifierProvider` has
   `NotifierConformance`, a pytest class with one test per obligation. A test fails a
   contract with no kit. The `cache`, `objectstore` and `taskqueue` contracts stay
   private until theirs exist. The kit is itself tested against broken drivers.
7. **`CapabilityRef[P]`, not `type[T]`.** `capability(REF, name) -> P`. mypy rejects an
   abstract class or a Protocol passed as `type[T]`; the key mirrors `ContractRef[P]`.
8. **Experimental means refused in production.** Configuring an experimental capability
   there needs the accepted risk `experimental_capability` (SANDLOOM-B023, ADR-0006). A
   documentation label alone does not hold: PEP 411 records that.
9. **A module cannot import a driver.** C004 reads installed `sandloom.drivers` entry
   points and refuses their modules, so a package keeps its reference and Protocol in a
   separate `contracts` module. `redis` and `celery` join the banned backend SDKs.

## Consequences

- Removed: `register(registry)`, `InfraRegistry` from the driver API, and the `s3`, `gcs`
  and `yc` placeholders. Selecting those names is the ordinary "no driver" error, naming
  what to install. Nothing is kept for compatibility: Sandloom is unreleased.
- `graph_store`, `workflow`, `search`, `lock` and `secrets` remain defined and not
  public. Each becomes public with its first driver and kit, as planned for a Temporal
  `workflow` driver.
- Rejected, with reasons in the research: eager loading in sorted order, an override
  flag, wrappers over providers, `TypeForm`, a `sandloom.experimental` namespace, and an
  experimental comment marker in modules, which a static check cannot resolve for a
  reference imported from another package.
