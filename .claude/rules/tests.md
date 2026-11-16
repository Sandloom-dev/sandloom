---
paths:
  - "src/sandloom_*/**/tests/**"
  - "examples/*/*/modules/**/tests/**"
---
# Tests

Copy-paste examples for every fixture, auth headers, rollback, events and
contracts: `docs/reference/testing.md`. Run `uv run sandloom test <module>` while
iterating. Inside a tracked wave, run the tests of what you touched plus every
test that references the changed symbols (`grep -rln '<symbol>' src tests`) and
the static gates; `uv run sandloom test --full` runs once per wave, by the
orchestrator (alone: once at the end). Never a serial `pytest -q` after each fix.

- **Imports.** Module tests import only `sandloom.sdk` (`sandloom.sdk.testing` for the
  kit), the generated stubs and DTOs, and their own package (C013). Seed
  other modules through their HTTP API, contracts or events, never their code.
  One more path is supported: a driver package's `testing` module
  (`sandloom_redis.testing`, e.g. `flush_owned_redis`) for that driver's own
  test resources. The SDK has no seam for it, and none is added.
- **The kit** (`from sandloom.sdk.testing import ...`; fixtures come from the
  `sandloom.sdk.testing.plugin`):
  - `client` shares the rolled-back `db`, so a 4xx does not roll back what
    the request wrote; `real_uow_client` uses real request units;
  - `user_id, headers = as_user(tenant_id=...)` mints a JWT user;
  - `published(E)` lists the `E` events the code published;
  - `await deliver(E(...), times=2)` drives the registered consumer runtime
    (schema check, durable claim, tenant binding), not only the handler;
  - `with fake_contract(REF, impl):` serves a contract you call;
  - `with fake_access_fact(name, value, owner="billing"):` fakes an entitlement
    of its owner module (an `Entitled(name, owner=...)` of another owner never sees it).
  - Never create identity rows from another module's tests.
- **Environment.** `.env.test` is loaded for you.
  - Use your own resources on a shared machine: `SANDLOOM_TEST_OWNER`,
    `SANDLOOM_TEST_DATABASE_URL`, and `SANDLOOM_TEST_CACHE_URL` /
    `SANDLOOM_TEST_BROKER_URL` for the cache and task queue the app selects (the
    harness merges the two Redis ones into the `url` of each Redis instance in
    `INFRA`, and refuses one whose capability the app does not select).
  - Never `pkill` by pattern.
- **Gate on `settings.is_prod`, never on `is_test`.** A `DeprecationWarning`
  from `sandloom.*` is an error.
- **Savepoints.** After a service call that may roll back a savepoint,
  snapshot `entity.id` first.
- More: `docs/reference/testing.md`.
