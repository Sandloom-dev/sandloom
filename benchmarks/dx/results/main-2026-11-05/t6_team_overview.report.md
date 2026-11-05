The `team_overview` module is built in `teamhub/`, and `sandloom test` passes on every step: tests, ruff, mypy, `sandloom check`, the snapshot checks and migration drift. I didn't run the app or the migrations against a live server. I also didn't run `--full`, which the module guide suggests for the end.

**1. What I built**
- **App:** `teamhub`, created with `sandloom new` using the identity, tenancy and billing modules.
- **Route:** `GET /api/v1/team_overview/` returns the current organization's members (user id and role), a `next_cursor`, and the plan (`plan_code`, `status`). It takes `limit` (1–100, default 50) and `after` for keyset paging.
- **Access:** only members of the organization can call it. The gate is `OrgRole("member")`, checked live through tenancy, so a removed member with a stale token gets 403.
- **Members:** these come from a local table, `team_overview_members`, that follows tenancy's `member_added`, `member_role_changed` and `member_removed` v2 events. Each page is one indexed query, however large the organization. It needs no tenancy call per request, so it keeps working when tenancy and billing are separate services. A removal is a tombstone, so a late older event can't bring a member back. Row-level security is enabled in the migration.
- **Rebuild:** `sandloom projection rebuild team_overview.members` refills the table from tenancy's membership contract.
- **Plan:** read live from billing's `subscription_claims` once per page. Billing's events carry no version, so they can't feed the local table. An organization with no billing customer shows status `none`.
- **Tests:** they cover events delivered out of order and twice, tombstones, other organizations being invisible, paging, the `limit` cap (422), a non-member (403), no login (401), the rebuild, and the plan fallback.
- **Scaffold cleanup:** I removed the sample item model, service and event that `sandloom add module` generated.

**2. What I read**
- The generated `AGENTS.md`, plus `teamhub/modules/AGENTS.md` (the module contract) and the scaffolded module files.
- `sandloom new --help`, `sandloom docs contracts` and `sandloom docs recipes`.
- The recipes `projection-from-platform-events` and `org-gate`.
- `sandloom docs tenancy.membership.v1`, `billing.entitlements.v1`, `tenancy.member_added.v2` and `OrgRole`.

**3. Mistakes and what caught them**
- Right after scaffolding, the first `sandloom generate` hadn't run because `celery` was missing. `uv sync` and then `generate` fixed it.
- I added a `tenant_id` index to my hand-written migration that the model doesn't declare. The migration drift gate (`SANDLOOM-T006`) failed, and I removed the index.
- The sample module's generated event schema file had to be deleted by hand once I removed the sample event.

**4. Where I guessed**
- **Recipe adaptation:** the "current organization" is the tenant bound from the user's token. I extended the recipe with the `OrgRole("member")` gate, a keyset cursor and typed response models.
- **Hand-written migration:** I edited the baseline migration directly instead of running `sandloom migrate new`, since nothing is deployed yet.
- **Billing fake:** I only tested against a faked billing contract, so the real `plan_code` and `subscription_status` values are unchecked beyond what the docs say.
