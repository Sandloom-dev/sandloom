Organization admins can now resend a pending invitation, and `sandloom test invites` passes: 9 module tests, the 22-test suite, and all the gates.

**1. What I built**
- **Route:** `POST /api/v1/invites/invitations/{invitation_id}/resend`, in `api.py`. It needs the `admin` role (`ADMIN = require(OrgRole("admin"))`), so members get 403 and anonymous callers get 401.
- **Service:** `services.resend_invitation` in `services.py` locks the pending row and replaces `token_hash` with a new token's hash, so the old link stops working. It renews `expires_at` to 7 days and republishes `InvitationSent` with the new token. The mail service picks that event up.
- **Not found:** it returns 404 for an unknown, revoked, accepted or other-organization invitation.
- **Expired invitations:** a pending invitation that has already expired can also be resent, which renews it.
- **Tests:** five new tests in `tests/test_invites.py` cover:
  - an admin resend, including the new token and expiry
  - the old token hash being replaced in the row
  - renewing an expired invitation
  - members and anonymous callers being refused
  - 404 for foreign, revoked and missing invitations, with no extra event published
- **Generated files:** I ran `sandloom generate`, which updated `routes.snapshot.json`.

**2. What I read**
- `teamhub/AGENTS.md` and `teamhub/modules/AGENTS.md`
- the invites module: `api.py`, `services.py`, `events.py`, `schemas.py`, the model, `__init__.py` and the existing tests
- the `org-gate` and `retries` recipes from `sandloom docs recipe`
- `activity/consumers.py`, to see who consumes `InvitationSent`
- `example/api.py`, for how a 404 is raised

**3. Mistakes and what caught them**
- My new tests read `Invitation` rows directly from the `db` fixture with no tenant bound. Three tests failed with `MissingTenantError` (SANDLOOM-R001). I fixed them by wrapping those reads in `bind_tenant(org)`.
- The first `sandloom test` ran past my 120-second timeout and went to the background, so I waited on its output file. The truncated summary hid the failure details, so I reran with `--no-gates --no-shuffle` and grepped the output.
- `ruff check` auto-fixed one lint error in my test file. I didn't note what it was.

**4. Where I guessed**
- **Role:** I read "organization admin" as `OrgRole("admin")`. That also lets owners through, since owner ranks above admin.
- **Event actor:** the republished event's `invited_by` is the admin who resent it, not the original inviter. This makes the activity feed attribute the resend to them, and it records an "Invitation sent" entry again.
- **Event reuse:** I reused the existing `InvitationSent` event instead of adding a new `InvitationResent` one. That keeps the mail service and activity consumer working unchanged, but the feed can't tell a resend from a first send.
- **`bind_tenant` import:** the tests use `bind_tenant` from `sandloom.sdk.tenant`, marked `# sandloom: advanced`. I didn't find a beginner-level way to read tenant rows in a test, so that import is a judgment call.
