---
name: sandloom-quality-gates
description: Run all Sandloom quality gates (tests, lint, types, architecture) and fix failures. Use before declaring any backend change done, or when asked to "check", "verify", or "запусти проверки".
---

# Sandloom quality gates

Run the gates from the repository root; every one must pass before work is done.
`uv sync` installs the exact tool versions pinned in `pyproject.toml` /
`uv.lock` (the same ones CI and the pre-commit hooks use).

## Steps

1. Ensure test services are up:
   ```bash
   docker compose -f deploy/compose/test.yml up -d --wait
   ```

2. Run the gates. One command runs the tests and every static gate:
   ```bash
   uv run sandloom generate        # after a declaration change: snapshots, stubs, DTO copies
   uv run sandloom test <module>   # while iterating: module tests, fast suite (parallel), gates
   uv run sandloom test --full     # once, at the end: the full suite in parallel, plus the gates
   ```

   - **What `sandloom test` runs:**
     - the module's tests;
     - `pytest -q -m "not slow" -n auto --dist loadfile` (with
       `SANDLOOM_TEST_REDIS_DB_STRIDE=auto`);
     - `ruff check`, `ruff format --check`, `mypy`, `sandloom check`, the
       architecture tests, `lint-imports`, the contract and route snapshots
       and the public API baseline;
     - the generated files (`module.snapshot.json`, contract stubs and DTO
       copies, exported event schemas), only checked: a breaking change fails
       with its code, a stale file with "generated files are stale: run
       `sandloom generate`". `sandloom test` never writes them.
   - **Summary.** The summary lists each step and exits 1 if any failed.
   - **Options.** `--plan` prints the steps, and `--no-gates` runs the tests
     only.
   - **Never re-run a serial `pytest -q` after each fix.** Re-run
     `sandloom test <module>`.
   - **A failing step names a code:** `uv run sandloom explain <code>` gives the
     why and the fix.
   - **Not covered:** the benchmark security suite (CI job
     `benchmarks-security`, `tools/ci/ci_local.sh <ref> --jobs
     benchmarks-security`).
   - **CI.** The pre-push hook and CI run everything anyway.
   - **Fixtures and seams:** `docs/reference/testing.md`.

3. Fixing guidance:
   - **ruff**: prefer real fixes; `**/tests/**` already ignores E501.
   - **mypy**: never add blanket ignores; tests and migration chains are
     excluded by design.
   - **architecture failures**: the error message explains the rule and the
     remedy (events / shared DTOs / registry contracts); `sandloom check
     --list-rules` lists the rules. `ORM_LEAKAGE_BASELINE` is remove-only.
   - **test DB errors** (`connection refused :5433`): test services are not
     up — see step 1.

4. Anti-patterns that will fail review even if gates pass:
   - importing one business module from another;
   - cross-module ForeignKey in models;
   - unversioned event channels (must match `<domain>.<event>.vN`);
   - sqlalchemy imports in `api/` or `schemas/` files;
   - `db.commit()` / `db.rollback()` in module code;
   - an import from `sandloom.core` or an `sandloom.sdk._*` package in module code;
   - state on a `@contract` class, or reaching `core.event_bus` /
     `core.infra` / `.raw_client()`;
   - a hand-edited generated file (`sandloom/shared/contracts/*`,
     `module.snapshot.json`): `sandloom generate` rewrites it.
