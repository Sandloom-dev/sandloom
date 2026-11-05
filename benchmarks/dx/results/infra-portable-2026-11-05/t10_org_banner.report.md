The `banner` module is built inside a new `teamhub` app, and the full `uv run sandloom test` passes: tests, ruff, mypy, `sandloom check`, snapshots and migration drift. I did not commit or push.

## 1. What I built
- **App:** `teamhub` was created with `sandloom new teamhub --with identity,tenancy,billing --no-git --yes --agents claude --services none`.
- **Routes:** there are three routes under `/api/v1/banner`. The organization is the caller's bound tenant (`X-Tenant-Id`), not a path parameter.
  - `GET` is open to any member (`OrgRole("member")`).
  - `PUT` sets the banner and `DELETE` clears it, both admin-only (`OrgRole("admin")`).
  - The text is trimmed, 1–280 characters.
- **Table:** one `TenantScoped` row per organization in `banner_org_banners`, with `tenant_id` unique and row-level security enabled in its migration. Clearing sets `text` to null and keeps the row.
- **Who and when:** `updated_by` and `updated_at` are on the row and returned by every route. Each set or clear also writes a `record_audit` entry (`banner.set` / `banner.cleared`) with the actor.
- **Tests:** `banner/tests/test_banner.py` has 6 tests.
  - Admin set, read, replace and clear.
  - Members get 403 on `PUT` and `DELETE`.
  - Two organizations can't see or change each other's banner.
  - An outsider gets 403.
  - Text validation returns 422.
  - Unauthenticated requests get 401.
- **Migration:** a fresh revision, and the migration-match test passes.

## 2. What I read
- `sandloom new --help`
- `teamhub/AGENTS.md`
- `teamhub/modules/AGENTS.md`
- `sandloom docs recipes`, in particular the `org-gate` recipe
- `sandloom docs` for `OrgRole`, `require`, `record_audit`, `WallClock` and `tenancy.membership.v1`
- The scaffolded example module: `api.py`, `services.py`, the model, the baseline migration and its tests

## 3. Mistakes and what caught them
- **Garbled model line:** I wrote a nonsense `updated_by` line in the model. I noticed it myself and rewrote the file before running anything.
- **Lint failures:** a long line in the model and in the schemas failed `ruff check`, and `ruff format` failed too. `sandloom test` caught both.
- **Scaffold's example files:** I deleted the scaffold's baseline migration for `banner_items`, so the module has a single migration. I had to re-run `sandloom generate`.

## 4. Where I guessed or got stuck
- **Org from the tenant, not the path:** I assumed it was safer to take the organization from the bound tenant than from `/orgs/{org_id}`. With a path parameter, `OrgRole` would read the organization from the URL while the data is filtered by the tenant, and I didn't verify that they must match.
- **No billing gate:** the request didn't mention subscriptions, so I left billing out of the gates. Say if you want `SubscriptionActive` added.
- **Cross-org test is a simulation:** `fake_contract` stands in for tenancy's membership contract. I did not run it against real tenancy data, so the "admin of A acts in B" case is covered by someone with no role in B getting 403. Isolation of the data itself comes from `TenantScoped` and row-level security, which the test database exercises.
- **Race on first set:** two simultaneous first-time sets in one organization would hit the unique constraint and return an error rather than merge. I left that unhandled.
- **Warning on every run:** pytest prints a harmless "settings imported before `.env.test`" warning, and I left it alone.
