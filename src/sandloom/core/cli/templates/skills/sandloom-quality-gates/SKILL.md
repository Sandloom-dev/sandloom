---
name: sandloom-quality-gates
description: Run every gate of this Sandloom app (tests, lint, format, types, sandloom check, generated files, migration drift) and fix failures. Use before declaring a change done, or when asked to "check", "verify" or "run the tests".
---

# Quality gates of <app>

Run from the app root. Every step must pass before work is done.

1. **Services.** `docker compose -f deploy/docker-compose.dev.yml up -d
   --wait` starts Postgres and Redis (the test ones on :5433 / :6380, as
   `.env.test` expects). On a shared machine use your own database and
   Redis: `SANDLOOM_TEST_DATABASE_URL`, `SANDLOOM_TEST_CACHE_URL`,
   `SANDLOOM_TEST_BROKER_URL`, `SANDLOOM_TEST_OWNER`.
2. **One command** runs the tests and every gate:

   ```bash
   uv run sandloom generate        # after a declaration change (snapshots, stubs)
   uv run sandloom test <module>   # while iterating
   uv run sandloom test            # once, at the end: the whole app
   ```

   - It runs: the generated files (only checked: a stale one fails with
     "run `sandloom generate`"),
     `schema & deploy`, the tests in parallel, `ruff check`, `ruff format
     --check`, `mypy`, `sandloom check`, the contract and route snapshots and
     the migration drift check. `--full` adds the slow tests; `--plan`
     prints the steps.
   - Never re-run a serial `pytest -q` after each fix: re-run
     `sandloom test <module>`.
   - A failing step names a code: `uv run sandloom explain <code>` says why and
     how to fix it.
3. **Fixing.**
   - `ruff`: fix the code (`uv run ruff format .` formats); tests already
     ignore E501.
   - `mypy`: no blanket ignores; tests and migrations are excluded.
   - `sandloom check`: the message names the rule and the remedy (events,
     contracts, DTOs in `<app>/shared`).
   - Migration drift (SANDLOOM-T006): `uv run sandloom migrate new <module>
     "<message>"`, then review the revision.
   - Generated files (`module.snapshot.json`, `routes.snapshot.json`,
     `<app>/shared/contracts/*`, `<app>/shared/events/*.json`): never edit
     them; review what `sandloom generate` wrote. A breaking change fails
     before anything is written (SANDLOOM-T005 events, SANDLOOM-T007 stubs,
     SANDLOOM-C012 DTO copies, SANDLOOM-T001 routes: approve a weaker route
     with `sandloom routes snapshot --write`).
4. **Fails review even when the gates pass:**
   - one module importing another (use events, contracts, projections);
   - a cross-module foreign key;
   - `db().commit()` / `rollback()` in module code;
   - an import from `sandloom.core` or `sandloom.sdk._*`;
   - state on a `@contract` class, or a raw backend client.
