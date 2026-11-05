I built `invites` and `quotas` in a new app `teamhub`, and the full `sandloom test` gate passes (18 tests, lint, types, snapshots, migration drift). It was scaffolded with identity, tenancy and billing, which I didn't otherwise use.

**1. What I built**
- **`quotas` module:**
  - **Contract:** a `quotas.consume.v1` contract with one keyed command, `consume(organization_id, quota, units, operation_key)`. The default limit is 50 per month for `invitations`, held in a `DEFAULT_LIMITS` constant, so there is no per-organization override yet.
  - **How it counts:** it keeps one counter per organization, quota and UTC calendar month. The consume step is a single conditional UPDATE, so concurrent consumers can't exceed the limit. A retry with the same key returns the original result and consumes nothing, even if the quota has filled since.
  - **Errors:** 429 with a clear message when the quota is used up, 409 if a key is reused for a different amount, 422 for bad input, and 403 for a different organization.
  - **Usage route:** `GET /api/v1/quotas/usage/{quota}` reports this month's usage.
- **`invites` module:**
  - **Routes:** `POST /api/v1/invites/invitations` and `GET /api/v1/invites/invitations` (newest first, capped at 100). Both require organization membership.
  - **What it stores:** one invitation per (organization, lowercased email), recorded only. Sending also publishes an `InvitationSent` event that carries no email.
  - **How it consumes the quota:** it calls `quotas.consume`, keyed on organization and a hash of the email. A retry, with or without an `Idempotency-Key` header, returns the existing invitation and consumes nothing. An email that was already invited still gets its invitation back after the quota is full.
- **Tests:**
  - **`quotas`:** 9 tests covering the limit, retries, the month rollover, per-organization separation and error cases.
  - **`invites`:** 5 tests including the 51st-invitation refusal, run against the real quotas module with only membership faked.
  - **Fixed `tests/test_smoke.py`:** it pointed at the removed sample route.

**2. What I read**
- `sandloom new --help`, `AGENTS.md`, and `teamhub/modules/AGENTS.md`.
- `.claude/skills/sandloom-new-module/SKILL.md`.
- `sandloom docs` for the `retries`, `call-contract`, `org-gate` and `test-service-or-task` recipes.
- `sandloom docs` for `billing.credits.v1`, `OrgRole`, `operation_key`, `WallClock` and `tenancy.membership.v1`.
- `sandloom explain SANDLOOM-T002`, plus the scaffolded sample module's code.

**3. Mistakes and what caught them**
- **Import path:** I imported the quotas stub from `sandloom.shared.contracts`, but this app's own contracts live under `teamhub.shared`. The module failed to load, which caught it.
- **Stale contract stub:** the scaffolded `quotas` contract stub was in the way. `sandloom generate` refused with SANDLOOM-T004 and T007 (breaking contract change). I deleted the unreleased stub and snapshot and regenerated.
- **Migration chains:** the first migrations kept the scaffold's sample table and then dropped it. The T002 check doesn't read drops, so the gate failed. I regenerated one clean baseline per module.
- **Smoke test:** the old sample-route test returned 404 once the sample route was gone. The gate's test suite caught it.
- **Lint and format:** `ruff` found a long line and a formatting issue, which I fixed.
- **Wasted wait:** I spent a long time waiting on `pgrep -f "sandloom test"` watchers that were matching other sessions' processes.

**4. Where I guessed or got stuck**
- **Repeat invites:** I chose that re-inviting an address returns the existing invitation instead of an error, so retries are free without relying on the header.
- **Month boundary:** it is UTC.
- **Refusal status:** I picked 429.
- **Order of steps:** consuming the quota and recording the invitation are not atomic, since a contract command commits on its own. The stable key makes a retry after a failure between them free. A failure there would still leave one unit spent without an invitation, until the same address is retried.
- **Gate timing:** it was slow on the shared machine, and background output stayed empty until the end, so I redirected it to a file.
- **No other modules:** I didn't wire invites to billing or tenancy beyond the membership check.
