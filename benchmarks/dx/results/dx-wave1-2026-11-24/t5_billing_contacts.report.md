Organization admins can now add, list and remove billing contacts in the workspace module, and `uv run sandloom test` passes on every step.

**1. What I built**
- **Endpoints** (admin only, `OrgRole("admin")`; members get 403):
  - `POST /api/v1/workspace/billing-contacts` takes a name and an email and returns 201.
  - `GET /api/v1/workspace/billing-contacts` lists the contacts, newest first.
  - `DELETE /api/v1/workspace/billing-contacts/{id}` returns 204, or 404 if the contact isn't in the caller's organization.
- **Isolation:** the new table `workspace_billing_contacts` is tenant-scoped, and the migration enables row-level security on it. An organization only sees and changes its own rows, and a delete from another organization gets 404.
- **Duplicates:** an organization can have several contacts. Adding the same email again, in any letter case, returns the existing contact instead of creating a second one. Emails are lowercased on save.
- **Production data:** the schema change is one new table in a new migration (`9fd6b2805c6e_billing_contacts.py`), so existing data isn't touched.
- **Tests:** five new tests in `tests/test_workspace.py`. They cover the add/list/remove flow with ordering, duplicates, invalid input (422), member 403s, and isolation between organizations.

**2. Files and docs I read**
- `AGENTS.md` and `teamhub/modules/AGENTS.md`.
- The workspace module's `api.py`, `schemas.py`, `services.py`, models, baseline migration and tests.
- `sandloom docs recipes`, specifically `list-order` and `org-gate`.
- `sandloom docs sandloom.sdk.errors`.

**3. Mistakes and what caught them**
- I left the new files unformatted, and the `ruff format` step in `sandloom test` failed. I ran `ruff format` and the rerun passed.

**4. Where I got stuck or guessed**
- **404 handling:** I found no SDK helper for "not found", so I raised FastAPI's `HTTPException` directly in the route. The module guide says "no error handlers", and I read that as not covering a plain route-level raise.
- **Duplicate emails:** I chose to return the existing contact rather than a 409. That was my call, not something the request specified.
- **Billing gate:** I didn't add a subscription check, since the request only asked for admin rights.
- **Email validation:** I used a simple regex because I didn't check whether the SDK offers an email type.
