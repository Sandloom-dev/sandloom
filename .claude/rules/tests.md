---
paths:
  - "**/tests/**"
  - "**/test_*.py"
---
# Tests

Copy-paste examples for every fixture, auth headers, rollback, events and
contracts: `docs/reference/testing.md`. Run `uv run sandloom test <module>` while
iterating and `uv run sandloom test --full` once at the end; never a serial
`pytest -q` after each fix.

- **Imports.** Module tests import only `sandloom.sdk` (`sandloom.sdk.testing` for the
  kit), the generated stubs and DTOs, and their own package (C013). Seed
  other modules through their HTTP API, contracts or events, never their code.
- **The kit** (`from sandloom.sdk.testing import ...`; fixtures come from the
  `sandloom.sdk.testing.plugin`):
  - `client` shares the rolled-back `db`, so a 4xx does not roll back what
    the request wrote; `real_uow_client` uses real request units;
  - `user_id, headers = as_user(tenant_id=...)` mints a JWT user;
  - `published(E)` lists the `E` events the code published;
  - `await deliver(E(...), times=2)` drives the registered consumer runtime
    (schema check, durable claim, tenant binding), not only the handler;
  - `with fake_contract(REF, impl):` serves a contract you call;
  - `with fake_access_fact(name, value):` fakes an entitlement.
  - Never create identity rows from another module's tests.
- **Environment.** `.env.test` is loaded for you.
  - Use your own resources on a shared machine: `SANDLOOM_TEST_OWNER`,
    `SANDLOOM_TEST_DATABASE_URL`, `SANDLOOM_TEST_CACHE_URL`, `SANDLOOM_TEST_BROKER_URL`.
  - Never `pkill` by pattern.
- **Gate on `settings.is_prod`, never on `is_test`.** A `DeprecationWarning`
  from `sandloom.*` is an error.
- **Savepoints.** After a service call that may roll back a savepoint,
  snapshot `entity.id` first.
- More: `docs/reference/testing.md`.
