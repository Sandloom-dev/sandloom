The invitations CSV export is in place, and `sandloom test` passes all ten gates: the suite, ruff, mypy, `sandloom check`, route snapshots and migration drift.

**1. What I built**
- **Route:** `GET /api/v1/invites/invitations/export` in `invites/api.py`. It is gated by `require(OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant()))`. Admins and owners pass, and the subscription is checked live on each request, not read from a cached claim. An unpaid organization gets 402 and a non-admin gets 403.
- **Service:** `export_invitations_csv()` in `invites/services.py`. It returns CSV with the columns `email,status,sent_at`, newest first, with `sent_at` taken from `created_at`. Rows are the caller's organization only (tenant scoping), capped at 10,000.
- **Formula guard:** emails that start with `=`, `+`, `-`, `@`, tab or carriage return get a leading `'`, so a spreadsheet won't run them as formulas. You didn't ask for this.
- **Tests:** three new tests in `invites/tests/test_invites.py`.
  - A paid admin and a paid owner both get the CSV, with no rows from other organizations and the formula guard applied.
  - A member, an outsider and an anonymous caller are refused with 403, 403 and 401.
  - An unpaid organization gets 402.
- **Generated files:** `sandloom generate` rewrote the route snapshot and `.sandloom/inspection.json`.

**2. What I read**
- `AGENTS.md` and `teamhub/modules/AGENTS.md`.
- `sandloom docs recipe org-gate`.
- The `sandloom docs` output for `Account` and `SubscriptionActive`.
- The invites module source and its existing tests.
- The framework's `account.py` in the installed package, to confirm what `Account.bound_tenant()` does.

**3. Mistakes**
- I tried `sandloom context invites` before `sandloom generate`, and it failed with SANDLOOM-T015 (no inspection index). The error message told me to run generate.
- I called Monitor without loading its schema. The tool rejected the call, and I switched to waiting on the background task.
- I also ran a polling loop that hit the 500-second tool timeout. The test run had already finished, so nothing was lost.

**4. Guesses**
- **Billing account:** the existing routes have no `org_id` in the path, so I used `Account.bound_tenant()` instead of the recipe's `Account.org(param="org_id")`. The docs only say it resolves to the caller's organization or personal account, so this is my reading of it. The tests fake the `organization.subscription.active` fact, so they don't exercise a real billing lookup.
- **Row cap and header:** the 10,000-row cap and the `sent_at` column name are my choices.
- **Doc pages:** `sandloom docs authorization` doesn't exist, so I couldn't read the authorization docs directly.
