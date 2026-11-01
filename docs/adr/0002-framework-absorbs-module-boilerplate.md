---
search:
  exclude: true
---

# ADR-0002: The framework absorbs per-module boilerplate

- **Status:** Accepted
- **Date:** 2026-10-09
- **Deciders:** Sandloom maintainers
- **Supersedes:** —

## Context

A business module is supposed to be *only its domain*. In practice every
module carried three pieces of framework plumbing, copied from the scaffold:

| File | Lines (linkly `links`) | What it did |
| --- | --- | --- |
| `tests/conftest.py` (+ an app-root `conftest.py`) | 131 + 19 | load `.env.test`, guard against non-test databases, build/reset the schema, override `get_db`, build an httpx client, mint JWTs, reset the Redis client singleton |
| `migrations/env.py` | 16 | name its own models module and table prefix for the Alembic runner |
| `__init__.py` | 39 | a `register(core)` that mounted the router and looped over event schemas inside `try/except` |

The copies drifted: the examples still used `drop_all` and `import sandloom.main`
while the scaffold template had moved on to `reset_schema` and
`load_composition_app()`. The conftests imported kernel internals
(`sandloom.core._internal.db_provider`, `sandloom.core.database`, the Redis driver's
private `_default_client`) — the exact coupling module isolation forbids in
production code, tolerated in tests only because nothing checked it.

## Decision

The framework owns this plumbing; modules declare, conventions derive.

1. **Test harness = an explicit pytest plugin.** `sandloom.core.testing` becomes a
   package (all existing names stay importable) with `plugin.py`, activated by
   the application once: `addopts = ["-p", "sandloom.core.testing.plugin"]`. It
   loads `.env.test` in `pytest_load_initial_conftests` — before any conftest
   can import `sandloom.core.config.settings`, which snapshots the environment — so the
   app-root conftest disappears as well. Fixtures: `sandloom_app`, `db_engine`,
   `db`, `client`, `make_headers`, `user_headers`, back-compat aliases
   `test_db` / `async_client` / `test_engine`, and per-test pool release.
   *No `pytest11` entry point*: installing the framework must not change the
   behaviour of any other suite, including the framework's own.
2. **Framework-tree modules** (scaffolded into `src/sandloom/`, whose suite
   does not load the plugin, and where `pytest_plugins` is not allowed in a
   non-root conftest) get a one-line conftest:
   `from sandloom.core.testing.fixtures import *`.
3. **Root cause over workaround.** The Redis cache driver's clients are
   loop-aware (rebuilt when the running loop changes), so no harness needs to
   poke a driver singleton.
4. **Migrations by convention.** `run_module_migrations(__file__)` derives the
   package, `<pkg>.models` and `<module>_` from the env.py location; keywords
   override.
5. **Declarative registration.** `register = define_module(__name__, ...)`
   mounts `<pkg>.api`'s router, registers `<pkg>.events.EVENT_SCHEMAS`, calls
   `setup(core)` for anything custom and returns the descriptor. Identical
   schema re-registration is a no-op (it already was; now pinned), so no
   `try/except`. A hand-written `def register(core)` keeps working.
6. **Enforced.** A new archcheck rule `test-imports` (run by `sandloom check` and the
   framework's architecture tests) forbids module tests from importing
   `sandloom.core._internal`, `sandloom.core.sql_alchemy`, `sandloom.core.database`,
   `sandloom.core.drivers`. Scaffold-output tests pin the sizes (conftest
   absent or ≤ 3 lines, env.py ≤ 3, `__init__` ≤ 8 non-blank lines) and that a
   fresh `sandloom new` project + scaffolded module passes its own tests.

## Consequences

- linkly's `links` module boilerplate went from 205 lines to 12
  (conftests 150 → 0, env.py 16 → 3, `__init__` 39 → 9).
- Harness fixes land once, in the framework, for every app.
- The bundled `identity` / `tenancy` / `billing` suites keep their bespoke
  conftests (Redis/SSO fixtures, ambient recording UoW, PSP fakes); they pass
  `test-imports` without allowlist entries (the legacy debt was paid off on
  2026-10-21).
- An application that predates this must add the `-p` addopt once; its
  existing conftests keep working (fixture names are compatible, and a local
  fixture overrides a plugin fixture of the same name).
- `define_module` is convention-bound: a module whose router lives elsewhere
  passes `router=`, one without an `events.py` simply has no schemas.

### Alternatives rejected

- **`pytest11` entry point** (auto-activation): changes unrelated suites on
  install and would silently alter the framework's own suite.
- **Conftest templates kept, just shorter:** still copies that drift, and a
  non-root conftest cannot load the env early enough for `settings`.
- **Frame-inspection-only `define_module()`:** works, but the explicit
  `define_module(__name__)` is greppable and robust; inference stays as a
  fallback.

## Related reading

- [Module model](../modules/index.md)
- [Testing harness](../modules/tooling/testing.md)
