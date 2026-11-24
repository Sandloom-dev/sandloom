The nightly purge is built and `uv run sandloom test` passes on all ten gates (tests, lint, types, checks and snapshots).

**1. What I built**
- A periodic task, `invites.purge-expired-invitations`, in `teamhub/modules/invites/tasks.py`. It runs at 03:00 UTC every day.
- It deletes invitations whose `expires_at` is more than 30 days in the past, across every organization. Deletion is in batches of 1000, with a separate committed unit for each batch.
- Running it twice is safe. It deletes by that condition, so a repeat or overlapping run finds nothing left to delete. If a worker dies partway, the batches already committed stay deleted and the next run finishes the rest.
- Tests are in `invites/tests/test_tasks.py`. They cover:
  - which rows are purged and which are kept
  - a repeated run deleting nothing more
  - batches being committed and looping until drained
  - the cron schedule and task id

**2. What I read**
- `teamhub/AGENTS.md` and `teamhub/modules/AGENTS.md`.
- The `sandloom docs recipe periodic-task` and `test-service-or-task` recipes. I followed the first closely.
- The invites module's `models`, `services.py` and existing tests.
- `sandloom add task --help`.

**3. Mistakes**
I made none that a check caught. `sandloom add task --periodic` scaffolded the task and updated the module snapshot, and `sandloom generate` found nothing further to change.

**4. Guesses and gaps**
- **Which invitations to delete:** I treated "expired" as `expires_at` alone, whatever the status. Accepted and revoked invitations are therefore also deleted once they are 30 days past expiry. If you want to keep accepted ones, add a status filter in `purge_batch`.
- **Separate beat:** `sandloom add task` printed a note that there is no `deploy/compose/microservices.yml`. If you split the app into services later, run `sandloom add deploy invites --service <name>` so the module gets its own beat.
