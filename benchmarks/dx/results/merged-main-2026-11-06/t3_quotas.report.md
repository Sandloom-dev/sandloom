`invites` and `quotas` are built, and `uv run sandloom test` passes on all steps. That covers the tests, ruff, mypy, `sandloom check`, the snapshots and migration drift. I couldn't test two concurrent requests racing; the guard against overshooting the limit is a single atomic database update, and nothing exercises it under concurrency.

## 1. What I built
The app `teamhub` was scaffolded with `sandloom new` and the bundled identity, tenancy and billing modules. I used the scaffold's sample module slot for `invites`.

**`quotas`**
- It publishes the contract `quotas.consume.v1` with `consume(organization_id, quota, amount, operation_key)` and `get_state`. It also has a `GET /api/v1/quotas/{quota}` route and a `QuotaConsumed` event.
- Usage is stored per organization, quota and calendar month (UTC). The `invitations` quota defaults to 50 per month.
- A consume is one atomic update that can't pass the limit. When the quota is used up it returns 429 with "the monthly invitations quota is used up; it resets next month".
- Each consume is recorded under a unique `operation_key`. A retry of the same key returns the original result without consuming again, even if the quota is full by then. The same key with a different amount or quota gives 409.
- Per-organization limit overrides are not built. Every organization gets the default.

**`invites`**
- `POST /api/v1/invites/orgs/{org_id}/invitations` and `GET` on the same path. Both are open to any member of the organization (gated with `OrgRole("member")`). The list is newest first and capped at 100 per request.
- Sending an invitation consumes one `invitations` unit, and a used-up quota gives 429. The address is recorded only; nothing is emailed.
- The invitation id is derived from the organization and the lowercased email, and the quota key is built from that id. A retry with an `Idempotency-Key` is replayed, a retry without one gets 409 because the address is already invited, and a retry after a crash between the two steps reuses the same quota key.
- Because of that id, an address can only be invited once per organization, ever.
- Consuming the quota and recording the invitation are separate steps. A crash between them wastes no extra unit on retry, but nothing rolls the first step back.

**Tests**
- `quotas`: counting down, refusal at the limit, a retry after the limit is reached, key conflicts, bad input, month rollover, separation between organizations and the event.
- `invites`, with a faked quota: the happy path and listing, an invalid email, a used-up quota, a retry never consuming twice, a retry after a crash, non-members refused, organization scoping and authentication.
- One `invites` test uses the real `quotas` module and checks that the 51st invitation is refused.
- I also changed `tests/test_smoke.py` to check the new routes instead of the sample route.

## 2. What I read
- `AGENTS.md`, `teamhub/modules/AGENTS.md`, the `sandloom-new-module` skill, and `sandloom new --help`.
- The docs recipes `retries`, `org-gate`, `call-contract` and `test-service-or-task`.
- `sandloom docs` for `billing.credits.v1`, `OrgRole`, `require`, `WallClock`, `operation_key` and `current_tenant_id`.
- The scaffolded sample module's code and tests.

## 3. Mistakes and what caught each one
- **Contract stub import path.** I imported the generated stub from `sandloom.shared.contracts`. The app's own stubs live in `teamhub.shared.contracts`; the module load error caught it.
- **Contract methods.** The scaffold's placeholder contract methods were still in the generated snapshot and stub. `sandloom generate` flagged their removal as a breaking change (SANDLOOM-T004/T007). I deleted the stub and snapshot and regenerated. I also had to hide `invites`' import for one run to break the circular dependency.
- **Reserved column name.** I first named a column `limit`, which is an SQL reserved word, and the first migration was generated with it. I renamed it to `max_units` and regenerated the migration after deleting the old one.
- **Editing with `sed`.** BSD `sed` rejected my multi-line edit, so I switched to Python for the edits.
- **Stale smoke test.** After removing the sample routes, `tests/test_smoke.py` failed with 404 instead of 401, and the suite step showed it.
- **Style.** Ruff reported one over-long docstring line and some formatting; I fixed both.
- **Scratch file location.** I wrote one scratch output file to `/tmp`, not the session scratchpad. It holds nothing needed.
- **Edits during a test run.** I edited test files while a background `sandloom test` was running, so that run's suite failure was partly noise. I reran it.

## 4. Where I got stuck or guessed
- **Bundled modules.** The brief said to use the bundled modules for accounts, organizations and billing. I installed identity, tenancy and billing, but billing is not used by the invites or quotas flow.
- **Error code.** I chose 429 for a used-up quota. Nothing in the docs says what Sandloom expects there; 402 is billing's code for "not enough credits".
- **Organization in the URL.** I used `/orgs/{org_id}/invitations` because `OrgRole` reads `org_id` from the path. I added a check that it matches the caller's bound organization, otherwise 403.
- **Email validation.** `email-validator` isn't installed, so I check email format with a simple rule instead of `EmailStr`.
- **Testing the month rollover.** I patched `current_period`, since I found no framework clock helper.
- **Tests of `quotas` without a request.** I tested `quotas` through its service functions with the `db_uow` fixture, as the recipe describes. I have not confirmed that the contract's own idempotency layer behaves correctly in a real request.
- **Migrations not applied.** I generated the migrations and the drift check passes, but I did not run `migrate upgrade-all --apply` against a database.
