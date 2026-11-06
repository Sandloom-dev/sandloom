A nightly task now deletes invitations that expired more than 30 days ago, in every organization. `uv run sandloom test` passes on all gates.

**1. What I built**
- **Task:** `teamhub/modules/invites/tasks.py` holds `purge_expired_invitations`, scheduled at 03:00 UTC through the beat hook that `sandloom add task --periodic` wires.
- **What counts as expired:** any invitation whose `expires_at` is older than 30 days, whatever its status (pending, accepted or revoked).
- **Safe to run twice or after a crash:** it deletes by that date condition in batches of 1000, one committed unit per batch. A repeated or overlapping run finds nothing left to delete, and a crashed worker's run is simply redone. The task is marked `acks_late` and `reject_on_worker_lost`, so Celery redelivers it if the worker dies.
- **Tests:** `teamhub/modules/invites/tests/test_tasks.py` has three tests:
  - a batch deletes only the long-expired invitations of several organizations and spares recently expired and still-valid ones, and a second batch deletes nothing;
  - the task itself commits and returns 0 on a repeat run;
  - the beat entry exists.

**2. What I read**
- `AGENTS.md` and `teamhub/modules/AGENTS.md`
- `sandloom docs recipes` and the `periodic-task` recipe
- `invites/models/invitation.py`, `invites/__init__.py`, `invites/services.py`
- the head of `invites/tests/test_invites.py`

**3. Mistakes**
- I left the new files unformatted, and the `ruff format` step of `sandloom test` failed. I ran `ruff format` and re-ran the gate, which passed.

**4. Stuck or guessed**
- I wasn't stuck. I read "expired" as `expires_at` alone, not as status. If you only want pending invitations purged, that's a one-line condition to add.
