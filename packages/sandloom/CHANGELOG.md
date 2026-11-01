# Changelog — sandloom

All notable changes to the `sandloom` distribution (named `sandloom-framework`
before 2026-10-28) are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Per-package entries are aggregated in the repository-level [docs/changelog.md](../../docs/changelog.md).

## [Unreleased]

## [0.7.0a5] — 2026-10-31

- **Breaking infrastructure API:** migrate string capability declarations and
  registration callbacks using the [a4 → a5 guide](https://sandloom.dev/docs/operate/upgrade/#upgrading-from-070a4-to-070a5).
- Include runtime container hardening, backup-identifier quoting, safe viewer
  links and the updated architecture explorer. Generate applications with
  `sandloom[redis,celery]>=0.7.0a5,<0.8`.

- Add `sandloom.driverkit` for driver packages, found by name in the `sandloom.drivers`
  entry-point group and imported only when selected (ADR-0011). Replaces `sandloom.infra`.
- Accessors return Protocols; `capability()` and `Capability()` take a `CapabilityRef[P]`.
- `sandloom generate` no longer opens a database when `SANDLOOM_TEST_DATABASE_URL`
  is set.
- `INFRA` no longer passes a default instance's options to a different driver.
- Add `Notification.body_fields`, the values a notification's body was built from.

## [0.7.0a4] — 2026-10-31

- Add cold project graphs, compact complete module maps and the offline Explorer:
  `sandloom graph`, `sandloom context MODULE` and `sandloom explore`.
- Inspect declared saga/route sequences, saga states, interface impact and
  file/Git architecture diffs, with stable JSON and readable text formats.
- Record module purposes, contract signatures, money risk, interface references
  and source navigation; ship the module-context agent skill.
- Route diagram edges around unrelated blocks, separate disconnected modules,
  preserve readable scale and explain concrete impact/diff changes.
- Bound the RPC compatibility self-test with one overall startup deadline while
  retaining guard detection and transport fallback.
- Generate applications with the `0.7.0a4` dependency baseline.

- Classify installed modules inside a project-local environment as external
  providers so cold inspection preserves source ownership.

## [0.7.0a3]

- Improve standalone CLI battery onboarding and link diagnostics to the exact
  error documentation. Add descriptions to configuration fields.
- Generate applications with the `0.7.0a3` dependency baseline.

## [0.7.0a2]

- Refresh the package description and project links on PyPI.
- Generate applications with the `0.7.0a2` alpha dependency floor.

## [0.7.0a1] — 2026-10-28

Initial public alpha release; earlier version headings describe internal development.

- Breaking (packaging): `sandloom` is the framework distribution, replacing
  `sandloom-framework`, `sandloom-shared`, `sandloom-platform`,
  `sandloom-infra-redis` and `sandloom-infra-celery` (removed, no shims). It
  ships the kernel, `sandloom.sdk`, `sandloom.shared` (including the
  standard modules' generated contract DTOs, stubs and event schemas), the CLI and
  `sandloom.main`. Extras: `redis`, `celery`, `standard` (Redis + Celery +
  the three module packages), `fast`, `check`, `testing`, `sentry`.
  Dependencies are ranges; `requests` and `python-multipart` are dropped,
  `itsdangerous` and `email-validator` moved to the module packages.
- Breaking: modules are discovered through the `sandloom.modules`
  entry-point group (formerly `sandloom.plugins`); the entry-point name is
  the module name. `sandloom.main:app` composes every installed entry point
  when `SANDLOOM_MODULES` is unset (`DEFAULT_MODULES` is gone). An ambiguous
  requested module name fails startup with SANDLOOM-B020. `MODULE_FS_SCAN`
  is removed (setting it fails startup).
- `sandloom new --with identity,tenancy,billing` depends on
  `sandloom[redis,celery]>=0.7,<0.8` plus one `sandloom-<module>` per module.
- `sandloom check`: importing an installed module package from an app module
  is SANDLOOM-C001.

- Breaking (kernel internals only, ADR-0007): `sandloom.core` is layered and
  acyclic, checked by import-linter; internal modules moved without
  re-exports (`sandloom.core.infra.drivers` → `sandloom.core.drivers`, consumer runtime
  → `sandloom.core.consumers`, `sandloom-migrate` → `sandloom.core.cli.migrate`, …). The
  public API is unchanged. The non-production default-registry fallback is
  removed (`di.cache()` / `di.notifier()` raise without a bound registry).
  See `CHANGELOG.md`.

- Preserve Boolean parameter coercion, deferred raiseload and root discriminator
  handling through conservative planner fallback to the ORM.

- Enable eligible optimized declared reads by default with an explicit rollback
  switch, conservative ORM fallback and enforced read-only transactions.
- Preserve application hooks/type processors/cardinality and scope transaction
  budgets; clean up failed or cancelled planner leases.
- Prevent contended pool starvation and stagger PostgreSQL recovery with capped
  exponential jitter, bounded waiting and a finite connection timeout.

## [0.7.0] — 2026-10-13

Internal development milestone; this version was not published to PyPI.
Distributions now follow the [Versioning & Stability](../../docs/contributing/versioning.md)
policy. The change list since 0.6.0 is in [CHANGELOG.md](../../CHANGELOG.md)
and the current source API upgrade steps are in
[Upgrading to Public API 1.0](../../docs/operate/upgrade-1.0.md).

### Fixed
- Atomic task-enqueue deduplication, local/RPC read memo parity, missing-path architecture validation, testing fixture export, and scaffold RLS/name validation.
- Source distributions include the package sources and build independently of the monorepo.

### Added
- Framework kernel: the enforced public surface (`sandloom.core.di`, `sandloom.core.facade`,
  `sandloom.core.module_api`, `sandloom.core.descriptors`, `sandloom.core.orm`,
  `sandloom.core.projections`) plus `sandloom.__init__` and the `sandloom` CLI (`new`,
  `scaffold`, `migrate`, `projection`, `version`).
- Lean kernel: no eager `redis`/`celery` imports; the Redis/Celery backends are
  opt-in via the `[redis]`, `[celery]` and `[infra]` extras.
- Pluggable infrastructure layer (`sandloom.core.infra`): capability contracts,
  registry, built-in filesystem/SQLAlchemy/log drivers, transactional outbox and
  audit stores.
