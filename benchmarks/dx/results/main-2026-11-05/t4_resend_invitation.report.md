Admins can now resend a pending invitation, and members can't. `uv run sandloom test` passes on all steps: tests, lint, format, types, check, generated files, route snapshots and migration drift.

**1. What I built**
- **Route:** `POST /api/v1/invites/invitations/{invitation_id}/resend`, gated by `OrgRole("admin")`. It returns the updated `InvitationOut`.
- **Service:** `resend_invitation` in `services.py` replaces the token hash, so the old link stops working. It sets a fresh `expires_at` (now plus 7 days), then publishes `InvitationSent` with the new token so the mail service delivers it again.
- **Errors:** an unknown invitation, or one in another organization, returns 404. An invitation that is accepted or revoked returns 409. A pending invitation that has already expired can still be resent.
- **Tests:** new ones in `test_invites.py` cover:
  - an admin resending, and the new event carrying a different token;
  - the stored hash matching the new token and not the old one;
  - a member getting 403 and an anonymous caller 401;
  - a cross-organization resend returning 404;
  - an unknown id returning 404;
  - a revoked invitation returning 409.
- **Generated files:** I regenerated them, including `routes.snapshot.json`.
- **Activity feed:** a resend publishes the same `InvitationSent` event, so the activity feed will show a second "Invitation sent to …" line. I left it that way rather than add a new event, but it's easy to change.

**2. What I read**
- `AGENTS.md` and `teamhub/modules/AGENTS.md`.
- The invites module sources and its tests.
- The activity module's consumer.
- `sandloom context invites`.
- `sandloom docs recipe org-gate` and `recipe test-service-or-task`.
- The example module's `api.py`, for how 404s are raised.

**3. Mistakes and what caught them**
- I typed `bash` instead of `Bash` on one call, and the tool errored.
- A first `ls` for the activity consumer failed because I'd guessed the wrong directory.
- I first imported `open_uow` from `sandloom.sdk.testing` in a test, a guess. The `test-service-or-task` recipe showed the `db_uow` fixture with `bind_tenant`, so I rewrote the test before running it.
- The first `sandloom test` failed on stale generated files and route snapshots, and on ruff line length and formatting. `sandloom generate`, `ruff check --fix` and `ruff format` fixed them. Two over-long lines are now wrapped.

**4. Where I guessed**
- Resending an already-expired but still pending invitation is allowed.
- Using `HTTPException` plus plain service exceptions for the error cases follows the example module. I found no framework error convention in the docs I read.
- I didn't check that `db_uow` shares the session with the request. The new DB-reading tests pass, so it appears to.
