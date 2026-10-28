---
name: sandloom-extract-module
description: Move one Sandloom module's existing data out of the shared (monolith) database into its own database with `sandloom migrate extract-module` — plan, fence, copy, verify, cut over, clean up. Use when a module that already holds production data is being split into its own service, or when asked to "extract", "move the data of", or "give <module> its own database".
---

# Extract a module's data into its own database

The data move is **offline, in a maintenance window**. The module's writers
are stopped while the other modules keep serving. The topology half of a
split (env anchor, migrate one-shot, bindings, gateway) is skill
`sandloom-split-deploy`. A new deployment with no data skips this skill entirely.
Full runbook: `deploy/README.md`, section "Runbook: moving a module's data out
of the monolith database".

Run every command with the **monolith's** configuration. Its `SANDLOOM_MODULES`
must load the module. `--from` / `--to` take an instance name or a URL.

## Steps

1. **Plan (dry run, the default, writes nothing):**
   `sandloom migrate extract-module <m> --from default --to "$<M>_DATABASE_URL"`.
   Read the plan:
   - the tables to copy;
   - the outbox, inbox and command-log rows to hand off;
   - the Streams groups to transfer;
   - any preflight refusal. The source chain must be at head and no foreign
     key may leave the module. RLS tables need a `BYPASSRLS` / superuser
     role.
2. **Stop every writer of the module:** the app replicas that load it, its
   Celery worker and beat, and its consumers. Nothing may write `<m>_*`
   tables or publish `<m>.*` events.
3. **Extract:** run the step 1 command with
   `--apply --i-have-stopped-writes` added.
   - It fences the source (read-only triggers), migrates the target to head,
     copies with per-table checksums, sets sequences, hands off undelivered
     outbox rows (same ids), copies the inbox, audit and command-log rows,
     and transfers consumer-group ownership.
   - A live writer makes it fail with "the module still has writers". Stop
     the writer and re-run the **same** command: every step is idempotent and
     resumes.
4. **Verify (any time):** run the same command with `--verify` (row counts
   and checksums against the manifest).
5. **Cut over:**
   - Start the module's service on its own `DATABASE_URL`, with the same
     `CACHE_URL`, bus and JWT settings.
   - Remove the module from the monolith's `SANDLOOM_MODULES`.
   - Add the `SERVICE_BINDINGS` and `RPC_ALLOWED_CALLS` entries.
   - Switch the gateway routes (`deploy/AGENTS.md`).
   - A stale monolith that still loads the module is refused at startup by
     the group-owner fingerprint. That is intended.
6. **Clean up, later and explicitly:** run the same command with
   `--drop-source --apply --i-have-stopped-writes`. It refuses while
   undelivered module events remain. It keeps the audit rows and the
   handed-off outbox rows.

**Rollback before the cutover:** `--unfence --apply`. After the extraction
has completed, this is refused without `--force`, because the new service
may already hold writes the source lacks.

## Checks

- `uv run pytest src/sandloom/core/tests/test_module_extraction.py -q`
  covers the fence, resume, refusals, drop-source, unfence and the group
  transfer.
- The full scenario is opt-in:
  `SANDLOOM_EXTRACTION_E2E=1 uv run pytest tests/extraction --no-cov`.
- Online (zero-downtime) extraction is **not implemented**. Plan a window per
  module.
