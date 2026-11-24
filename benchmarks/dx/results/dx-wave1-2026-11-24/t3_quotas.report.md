I've built both modules in `teamhub`, and `sandloom test` passes: 13 tests, plus lint, types, `sandloom check`, the snapshot checks and migration drift. That run was before I stopped, so I did no further manual checking.

**What I built**
- **`quotas`** owns the monthly limits. Its contract `quotas.quota.v1` has `status(organization_id, quota)` and `consume(organization_id, quota, amount, operation_key)`.
  - The default limit is 50 per calendar month (UTC), set by `QUOTAS_INVITATIONS_MONTHLY_LIMIT`.
  - Each `operation_key` is recorded once, so a repeat returns the first answer and consumes nothing. Reusing a key for a different amount is a 409.
  - The increment is guarded in SQL, so concurrent calls can't pass the limit. When the quota is used up the call fails with a 429 saying so ("invitations quota used up for 2026-11 (50 per calendar month)").
  - `GET /api/v1/quotas/{quota}` shows the organization's usage this month.
- **`invites`** has `POST /api/v1/invites/invitations` and `GET /api/v1/invites/invitations`, newest first. Both require organization membership via `OrgRole("member")`.
  - Email is validated and lower-cased, and there is one invitation per address per organization; a resend is a 409 and costs nothing.
  - Sending records the invitation, then consumes one `invitations` unit with the stable key `invite:{org}:{email}`. A 429 rolls the record back.
  - A retried request therefore never consumes twice, with or without an `Idempotency-Key` header.
- **Tests** cover send and list, per-organization isolation, non-members (403), no auth (401), a bad email, a retry with the idempotency key, a resend, and exhausting all 50 units. In that last test the 51st send gets a 429 and leaves no record, and another organization still has its own 50.
- **Sample code:** I replaced the scaffold's sample items and their baseline migrations, and updated `tests/test_smoke.py` to the new invites route.

The quotas contract itself has no direct tests; it is only exercised through invites (and the `GET /quotas/...` status route). A direct test of the 409 key-reuse and 403 wrong-organization cases is missing.

**Files and docs I read**
- `teamhub/AGENTS.md` and `teamhub/modules/AGENTS.md`
- `sandloom docs recipe retries`, `call-contract` and `org-gate`
- `sandloom docs` for `billing.credits.v1`, `tenancy.membership.v1`, `WallClock`, `TenantScoped` and `conflict_insert`
- The scaffolded sample module and billing's `credits.py` docstring, as the model for a keyed command

**Mistakes and what caught them**
- I left the scaffolded sample contract stub in place. `sandloom generate` refused with T007 (incompatible change); deleting the stub and regenerating fixed it.
- I used `conflict_insert` for bulk inserts into tenant-scoped tables. The framework refused at runtime (`UnsafeTenantOperationError`), so I switched to ORM objects in a savepoint and caught `IntegrityError`.
- I added a tenant-id index in the hand-written migrations that the unique constraints already cover. The migration drift check (T006) caught it.
- mypy rejected `.rowcount`, so I used `.returning()` instead.
- A modified row that wasn't flushed leaked across tenants in the shared test session (`TenantMismatchError`). I added an explicit flush.
- The old smoke test hit the removed sample route and got a 404 instead of 401, which I fixed.

**Where I guessed**
- The refusal is HTTP 429, since the spec only asked for a clear error.
- Duplicate invites are a 409 rather than silently succeeding.
- "Member" counts as allowed to invite; the spec didn't name a role.
- The month is taken from the server's UTC clock.
- The invitation is only recorded, with no email sent.
