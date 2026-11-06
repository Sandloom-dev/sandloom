I added the CSV export to the invites module, and `sandloom test invites` passes. That run covers the module's tests, the full fast suite, ruff, mypy, `sandloom check`, the generated-file checks and migration drift.

**(1) What I built**
- **Endpoint:** `GET /api/v1/invites/orgs/{org_id}/invitations.csv`, in `invites/api.py`. It returns `text/csv` with a download filename, and the columns are `email,status,sent_at`. `sent_at` is the invitation's `created_at` in ISO 8601 UTC. Rows are the organization's invitations, newest first.
- **Access check:** the route requires an admin-or-owner role and an active subscription.
  - The role check is `OrgRole("admin")`. It is a live membership check, and owners rank above admins.
  - The subscription check is `SubscriptionActive`, which asks billing on every request. I chose it over `Subscribed`, which reads a claim in the token and could be stale.
  - Without a subscription the response is 402, and without admin rights it is 403.
- **Cross-org requests:** the `org_id` in the path must match the caller's organization. A request for a different organization returns 403, so the billing check can't be pointed at one organization while the rows come from another.
- **CSV building:** `export_invitations_csv()` in `services.py` uses the `csv` module. It prefixes a `'` to emails that start with `=`, `+`, `-` or `@`, so a spreadsheet won't run them as formulas.
- **Tests:** four new tests in `invites/tests/test_invites.py`:
  - a paid admin gets the right content and ordering, with the formula-prefix case and no rows from other organizations;
  - member, outsider and anonymous callers are refused, and an owner is allowed;
  - subscription inactive gives 402, and the same admin gets 200 once it is active;
  - another organization's id gives 403.
- **Generated files:** `sandloom generate` refreshed the invites snapshots.

**(2) What I read**
- `AGENTS.md`
- `teamhub/modules/AGENTS.md`
- the `org-gate` recipe from `sandloom docs recipes`
- the invites module's `api.py`, `services.py`, `schemas.py`, `models/invitation.py` and `tests/test_invites.py`
- `sandloom docs Account`
- the framework's `shared/authz/_resolve.py` and `atoms_live.py`, to confirm what happens when the path `org_id` and the bound tenant disagree

**(3) Mistakes**
- **Wrong directory:** I `cd`'d into a relative path that didn't exist and got a "no such file" error. I switched to absolute paths.
- **Wrong tool name:** I called `bash` instead of `Bash`, and the tool call was rejected. I resent it with the right name.
- **Slow docs call:** one `sandloom docs` call timed out and moved to the background, so I read the framework source instead.
- Nothing failed at the gate after that.

**(4) Guesses and open points**
- **"Sent date":** the model has no separate sent-at field, so I used `created_at`.
- **Return type:** the route returns a bare `Response` rather than a pydantic DTO. `sandloom check` accepted it.
- **Size limit:** the export has no row cap. Large organizations would produce a big response, and streaming or a cap might be worth adding.
- **Billing check:** my tests fake the `organization.subscription.active` fact, as the recipe does. They never hit real billing, so the live lookup itself is untested here.
