I built the `invites` and `quotas` modules in `teamhub/`, and `sandloom test --full` passes. That run includes lint, types, `sandloom check`, the snapshots and migration drift. Nothing was committed.

## 1. What I built
- **App:** `teamhub`, created with `sandloom new --with identity,tenancy,billing`.
- **`quotas` module:** it stores per-organization, per-month usage and one row per consumption key.
  - **Limit:** `invitations` is 50 a month, set in `DEFAULT_LIMITS`. Other names are refused with 422.
  - **Contract:** `quotas.quota.v1` has one method, `consume(organization_id, quota, amount, operation_key) -> units left`.
    - A used-up quota answers 429 with an "exhausted" message.
    - The same key returns the original answer and consumes nothing, even when the quota is full.
    - The same key with another quota or amount answers 409.
    - Another organization answers 403.
  - **Concurrency:** the monthly usage row is locked, so two requests can't both pass the limit.
  - **Route:** `GET /api/v1/quotas` shows this month's usage.
- **`invites` module:**
  - **Routes:** `POST` and `GET /api/v1/invites/orgs/{org_id}/invitations`, open to any member of the organization (checked live through the membership contract).
    - The list is newest first and capped at 100.
    - A path `org_id` other than the caller's organization answers 403.
  - **Storage:** the email is lowercased and stored per organization, once. Nothing is sent.
  - **Quota:** sending calls `consume` before storing the invitation. The key is derived from the organization and the address.
- **Retries:** a retry with the same `Idempotency-Key` header is replayed by the framework. A retry without the header, or after a crash between the quota call and the insert, reuses the same key, so it consumes nothing more.
- **Migrations and tests:** both modules have their own migrations (with RLS). Tests cover each module: 7 for quotas and 8 for invites.

## 2. What I read
- **Guides and docs:** `teamhub/AGENTS.md` and `teamhub/modules/AGENTS.md`. The `sandloom docs` pages for `OrgRole`, `require`, `contract`, `command`, `operation_key`, `billing.credits.v1` and `tenancy.membership.v1`. The recipes `retries`, `call-contract`, `org-gate` and `test-service-or-task`.
- **Sample code:** the generated `example` module.

## 3. Mistakes and what caught them
- **Import path and generate order:** I first imported the contract stub from `sandloom.shared...`. Loading failed with a "no module" error. App-owned stubs live in `teamhub.shared.contracts`, and they only exist after `generate` runs. I removed the import, ran `generate`, then restored it.
- **`sed -i` on macOS:** the edits failed (macOS `sed` needs a suffix). I redid them in Python.
- **`find /`:** I ran a slow, needless filesystem search and stopped it.
- **Bulk inserts:** my first quota code used `INSERT ... ON CONFLICT` on tenant tables. The framework forbids that with `UnsafeTenantOperationError`, and 4 tests caught it. I rewrote it with ORM rows, savepoints and a row lock.
- **Lint and types:** an over-long docstring and two mypy errors were caught by `ruff` and `mypy`.

## 4. Where I guessed
- **429 for a used-up quota:** the brief only asked for "a clear error". 402 or 409 would also fit.
- **One invitation per address:** I added a 409 for inviting the same address twice. The brief didn't ask for it; it makes the quota key natural and avoids double consumption.
- **Role:** I let any member send invitations, because the brief said "a member".
- **Fixed limit:** the 50-a-month limit is a code constant, not per-organization configurable.
- **Calendar month:** it is taken in UTC.
- **Not atomic:** the quota call commits in `quotas`' own unit, so it is not atomic with the invitation insert. I accepted that and made the key deterministic so retries are safe. A lost insert leaves a consumed unit until the same address is sent again.
- **No concurrency test:** I did not write a test that races two consumptions. I only reasoned about the lock.
