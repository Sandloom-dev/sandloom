Organization admins can now add, list and remove billing contacts in the workspace module, and `sandloom test` passes on all gates.

**1. What I built**
- **Routes** (admin-only, under `/api/v1/workspace`):
  - `POST /billing-contacts` takes name and email and returns 201.
  - `GET /billing-contacts` lists the organization's contacts, oldest first, with a limit of at most 200.
  - `DELETE /billing-contacts/{id}` returns 204.
- **Isolation:** The new `workspace_billing_contacts` table is `TenantScoped`, and its migration enables row-level security. Every query runs inside the caller's organization. Removing another organization's contact returns 404.
- **Email handling:** Emails are lowercased and checked against a simple pattern. A duplicate email within one organization returns 409; the same email is allowed in a different organization.
- **Migration:** It only adds a table, so existing production data isn't touched. It is `eababe7ccab8_billing_contacts.py`, chained after `0001_workspace`.
- **Tests:** Four new tests in `tests/test_workspace.py`:
  - the add, list and remove flow
  - validation and duplicates
  - 403 for members
  - isolation between two organizations

**2. What I read**
- `teamhub/AGENTS.md`
- `teamhub/modules/AGENTS.md`, the module contract
- The whole workspace module: api, services, schemas, models, migration and tests
- The invites `api.py`, for the paginated-list style
- `sandloom docs recipes` (only the list of recipe names)

**3. Mistakes and what caught each**
- My `sed` on `schemas.py` failed on macOS (BSD `sed`), so `UUID` was never imported. `sandloom migrate new` failed with a `NameError` when it loaded the module. I fixed it with `sed -i ''`.
- Two files weren't `ruff format`-clean. The `ruff format` step in `sandloom test` caught it, and I ran `ruff format`.

**4. Stuck points and guesses**
- I used a regex for email validation because I wasn't sure `EmailStr` (which needs `email-validator`) is installed.
- I raise `HTTPException` from the service. I saw no domain-error convention in the module, so this is a guess.
- Admin-only for listing is my reading of "organization admins manage contacts"; members get 403 on all three routes.
- I didn't check the framework docs for a built-in way to handle duplicates or not-found errors.
