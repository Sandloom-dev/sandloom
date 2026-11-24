An organization admin can now resend a pending invitation, and `sandloom test invites` passes every gate, including ruff, mypy and `sandloom check`.

**(1) What I built**
- **Route:** `POST /api/v1/invites/invitations/{invitation_id}/resend`, in `api.py`. It is gated by `OrgRole("admin")`, so regular members get 403.
- **Service:** `resend_invitation` in `services.py`. It locks the row, overwrites `token_hash` so the old link stops working, and sets `expires_at` to now plus 7 days. It then re-publishes `InvitationSent` with the new token.
- **Mail and feed:** I reused `InvitationSent` because the mail service already delivers from that channel. The cost is that the activity feed records a second "Invitation sent" entry for each resend.
- **Errors:** an unknown invitation, or one from another organization, returns 404. An invitation that is accepted or revoked returns 409.
- **Tests:** six new ones in `tests/test_invites.py`:
  - an admin resends and gets a new token;
  - the stored hash is replaced and the old one is gone;
  - a member gets 403 and an unauthenticated caller gets 401;
  - an admin of another organization gets 404;
  - an unknown id gets 404;
  - a revoked invitation gets 409.
- **Generated files:** I ran `sandloom generate`, which updated `routes.snapshot.json`.

**(2) What I read**
- `AGENTS.md`
- `teamhub/modules/AGENTS.md`
- All of the invites module: `services.py`, `api.py`, `events.py`, `schemas.py`, the model, `settings.py`, `__init__.py` and the existing tests.
- The activity module's `consumers.py` and `services.py`.
- The `org-gate`, `retries` and `test-service-or-task` recipes from `sandloom docs recipe`.
- `sdk/errors.py`, to see what the SDK offers for 404 and 409 responses.

**(3) Mistakes and what caught them**
- `sandloom context invites` failed with SANDLOOM-T015, "no inspection index". I read the code directly instead, and `sandloom generate` fixed it later.
- My first tests queried `Invitation` straight from the `db` fixture and hit `MissingTenantError` (SANDLOOM-R001). The test run caught it. I fixed it by wrapping those queries in `bind_tenant(org)`, which is an advanced import and carries the required `# sandloom: advanced` comment.
- Ruff flagged my import order and formatting. `ruff format` and its autofix cleaned that up.

**(4) Where I guessed**
- The SDK has no domain-level 404 or 409 exception, so I used FastAPI's `HTTPException` in `api.py`, which the module already imports from. I couldn't confirm that is the convention.
- I assumed the existing mail delivery should be reused through `InvitationSent` rather than a new "resent" event.
- I did not add the `Idempotency-Key` replay behaviour or a test for it. I'm relying on the framework's automatic idempotency for writes.
