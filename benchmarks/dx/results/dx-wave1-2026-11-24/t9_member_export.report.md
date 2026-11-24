Admins and owners of a paid organization can now download its invitations as CSV from `GET /api/v1/invites/invitations/export`. `uv run sandloom test` passes on the full gate (tests, ruff, mypy, `sandloom check`, snapshots, migration drift).

**1. What I built**
- **Route:** the route is in `api.py` and is gated with `OrgRole("admin") & SubscriptionActive(of=Account.org(param=None))`. Admins and owners pass the role check. The subscription check runs live on every request. A non-admin gets 403, an unpaid organization gets 402, and no login gets 401.
- **Organization:** `param=None` follows the existing routes, which take the organization from the caller's bound tenant rather than a path parameter.
- **CSV:** `services.export_invitations_csv()` writes the columns `email,status,sent_at`, newest first, with `sent_at` taken from `created_at` as an ISO timestamp. It is returned as `text/csv` with an attachment filename.
- **Formula guard:** an email that starts with `=`, `+`, `-` or `@` gets a leading `'`, so a spreadsheet won't run it as a formula. The invite email pattern allows such addresses, so I added this.
- **Tests:** four new tests in `tests/test_invites.py`. They cover:
  - an owner exporting, with the content and ordering checked;
  - a member, an outsider and an unauthenticated caller being rejected;
  - a subscription that lapses between two requests, which flips 200 to 402 and shows the check ran twice;
  - one organization not seeing another's invitations.
- **Generated files:** `sandloom generate` rewrote the invites route snapshot.

**2. Files and docs I read**
- `teamhub/AGENTS.md` and `teamhub/modules/AGENTS.md`.
- The invites module: `api.py`, `services.py`, `schemas.py`, `models/invitation.py`, `tests/test_invites.py`.
- The generated stubs `billing_entitlements_v1.py` and `tenancy_membership_v1.py`.
- `sandloom docs recipes` and `sandloom docs recipe org-gate`.

**3. Mistakes and what caught them**
- I left the new code unformatted, and the `ruff format` step in `sandloom test` failed. I ran `ruff format` and the next run passed.

**4. Stuck or guessed**
- Nothing blocked me.
- I guessed that "sent date" means `created_at`, because the table has no separate sent column.
- I guessed the `sent_at` column name and the ISO timestamp format.
- The export returns every invitation with no row cap, which is fine at small scale. A very large organization would want paging or streaming.
