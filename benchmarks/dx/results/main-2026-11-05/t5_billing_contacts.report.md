Admins can now add, list and remove billing contacts in the workspace module, and `uv run sandloom test` passes on all steps.

**1. What I built**
- **Routes:** these live under `/api/v1/workspace/billing-contacts`.
  - `GET` lists the contacts, newest first.
  - `POST` adds one and returns 201. Both fields are required, the name is at most 120 characters, and the email must look like `a@b.co`.
  - `DELETE /{id}` removes one and returns 204. It returns 404 if the organization has no contact with that id.
- **Access:** all three require the organization-admin role. Members get 403, including on the list.
- **Isolation:** the table is tenant-scoped with row-level security. Every query runs inside the caller's organization, so another organization's contact id simply returns 404.
- **Several contacts:** an organization can have as many as it likes.
- **Production data:** the migration only creates a new table (`workspace_billing_contacts`), so existing data is untouched.
- **Files:**
  - Code: `models/billing_contact.py`, plus additions to `schemas.py`, `services.py` and `api.py`.
  - Migration: `migrations/versions/1bc97bb23ec9_billing_contacts.py`, which enables row-level security.
  - Regenerated files: the route snapshot and `.sandloom/inspection.json`.
  - Tests: four new tests in `tests/test_workspace.py`. They cover the add, list and remove flow with ordering and 404 on a second delete, validation, member 403s, and isolation between two organizations.

**2. What I read**
- `AGENTS.md`
- `teamhub/modules/AGENTS.md`
- The existing workspace module: models, services, api, schemas, tests and baseline migration
- The `sandloom docs recipe list-order` and `org-gate` recipes

**3. Mistakes and what caught them**
- I used GNU-style `sed` multi-line edits on macOS. They failed silently, leaving a missing `UUID` import. The failed `sandloom migrate new` caught it, and I redid the edits in Python.
- The formatting step of `sandloom test` failed. After I ran `ruff format`, the "generated files" check went stale because the formatter touched a generated file. Re-running `sandloom generate` fixed it.

**4. Where I guessed**
- **Routes:** I kept the existing workspace pattern, where the organization comes from the caller's session rather than an `org_id` in the path.
- **Email check:** it is a simple regex, not a strict email parser.
- **Duplicates:** the same email can be added twice to one organization. You didn't say to forbid that.
- **Audit:** I didn't add `record_audit` to the add and remove routes, and I didn't check whether the framework already audits them.
