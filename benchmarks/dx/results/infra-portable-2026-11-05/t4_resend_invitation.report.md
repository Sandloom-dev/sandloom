Org admins can now resend a pending invitation, and the full `uv run sandloom test` gate passes.

**1. What I built**
- **Route:** `POST /api/v1/invites/invitations/{invitation_id}/resend`, in `invites/api.py`. It needs an organization admin (`OrgRole("admin")`); a regular member gets 403 and an unauthenticated call gets 401.
- **Service:** `services.resend_invitation` in `invites/services.py` replaces the invitation's token hash, so the old link stops working, and sets a fresh 7-day expiry. It then publishes `InvitationSent` again, which the mail service delivers.
- **Not found:** it returns 404 if the invitation doesn't exist, belongs to another organization, or isn't pending.
- **Tests:** three new tests in `invites/tests/test_invites.py`:
  - an admin resend changes the token and extends the expiry, and the stored hash matches only the new token;
  - a member is refused (403) and an unauthenticated call is refused (401), with no extra event published;
  - another organization's admin, an accepted invitation, and an unknown id each get 404.
- **Regenerated:** the route snapshot (`routes.snapshot.json`) and `.sandloom/inspection.json`, via `sandloom generate`.

Side effect: the `activity` feed will log "Invitation sent to …" again on each resend, because it consumes the same event. In the resent event, `invited_by` is the admin who resent it, not the original inviter.

**2. What I read**
- `AGENTS.md` and `teamhub/modules/AGENTS.md`.
- The `invites` module source and its tests, plus `activity/consumers.py`.
- `sandloom context invites`.
- The `sandloom docs recipe org-gate` and `test-service-or-task` recipes.
- A grep of other modules for how they raise 404s.

**3. Mistakes and what caught them**
- I ran `uv run sandloom test` with unformatted code, and `ruff format` failed. I ran `ruff format` on the module.
- My new tests called `db()` outside a unit, which raised `UnitOfWorkRequiredError` (SANDLOOM-R002). The `test-service-or-task` recipe pointed me to the `db_uow` fixture plus `bind_tenant`.
- The generated-files check reported stale snapshots until I ran `sandloom generate`.
- I made one stray tool call (a malformed Monitor call) that the tool rejected and had no effect.

**4. Where I guessed**
- **404 vs 409:** I used 404 for "not pending", rather than 409 for an already-accepted or revoked invitation.
- **Expired invitations:** expired but still-pending invitations can be resent, since that is the main reason to resend.
- **Reusing the event:** I reused `InvitationSent` instead of adding a new event. A separate "resent" event would avoid the duplicate feed entry but means a new channel and a change on the mail service side.
- **Error style:** I raised `HTTPException` the way the `example` module does, since I found no framework-specific not-found helper.
- **Audit:** I did not add `record_audit`. The route's own audit is derived by the framework, and I didn't check what it records.
