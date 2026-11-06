A nightly job now deletes invitations that expired more than 30 days ago, across every organization. `sandloom test` passes in full, including the new tests, lint, types and the other gates. I didn't run the task against a real beat and worker.

**1. What I built**
- **Task:** `teamhub/modules/invites/tasks.py`, scaffolded with `sandloom add task invites purge_expired_invitations --periodic "0 3 * * *"` (03:00 UTC).
- **What it deletes:** invitations whose `expires_at` is before now minus 30 days, whatever their status or organization. An invitation that was accepted or revoked is deleted too once its expiry is old enough.
- **Safe to repeat:** it deletes by that condition in batches of 1000, with one committed unit per batch. A second or overlapping run finds nothing left to delete. If a worker dies halfway, the batches already committed stay deleted and the redelivered task finishes the rest (`acks_late` and `reject_on_worker_lost`).
- **Tests:** `teamhub/modules/invites/tests/test_tasks.py` covers three things. One test checks the cutoff, the cross-organization delete and that a repeated run deletes nothing. One runs the real task twice with commits. One checks the nightly beat registration.

**2. What I read**
- `teamhub/AGENTS.md` and `teamhub/modules/AGENTS.md`.
- `sandloom docs recipe periodic-task` and `sandloom docs recipe test-service-or-task`.
- The `invites` module: `models/invitation.py`, `services.py`, `__init__.py` and the head of `tests/test_invites.py`.
- The `sandloom add task --help` output.

**3. Mistakes**
- I wrote `tasks.py` with the scaffold's long lines unformatted. `ruff check` and `ruff format` in `sandloom test` caught it, and `ruff format` and `ruff check --fix` fixed it.

**4. Stuck or guessed**
- Nothing blocked me.
- I read "expired" as `expires_at`, not `status`. The model has no "expired" status, so that was the only sensible reading.
- I did not add a deploy-profile beat for a split deployment. `sandloom add task` reported there is no `microservices.yml`, and the monolith profile is covered by the gate.
