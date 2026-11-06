The `notes` module is built in the new `teamhub` app, and `uv run sandloom test` passes: tests, ruff, mypy, `sandloom check`, the snapshots and migration drift. I did not run `--full`. I didn't start the server or try it against real accounts or billing data. The tests fake the membership and subscription answers.

## 1. What I built

`sandloom new teamhub` created the app with the identity, tenancy and billing modules. It also scaffolded a sample `notes` module with an `items` example, which I replaced with notes. All routes are under `/api/v1/notes/orgs/{org_id}/notes`.

- **Operations:** members can create, list (newest first, capped at 100), read, edit (partial `PATCH`) and delete notes. A note has a title, a body, an author and created/updated times.
- **Isolation:** `Note` is `TenantScoped`, so another organization's note comes back as 404. Naming an organization you don't belong to gives 403, and so does an organization that isn't the one your token is bound to.
- **Announcement:** creating a note publishes `NoteCreated` on `notes.note.created.v1`. It carries the note id, organization id and author id, so search indexing or notifications can subscribe later.
- **Paid gate:** creating requires membership plus an active subscription on the organization (`SubscriptionActive`, checked live). Without one you get 402 and no event is published. Reading, editing and deleting only need membership.
- **Database and tests:** the baseline migration now creates `notes_notes`, with the tenant row-level security the sample used. There are 7 tests. They cover the full lifecycle, the event, list order and cap, sharing between members, cross-organization denial on every route, the paid gate, validation and 401s.

## 2. What I read

- `sandloom new --help`.
- `AGENTS.md` and `teamhub/modules/AGENTS.md`.
- The scaffolded sample module: every file plus its tests and migration.
- `sandloom docs recipes` and the `org-gate` recipe.
- `sandloom docs` entries for `Subscribed`, `OrgRole`, `authorize_resource`, `module_router`, `WallClock`, `UtcDateTime` and the tenant module.
- `sandloom explain SANDLOOM-T015`.

## 3. Mistakes and what caught them

- **Redundant guard:** I added my own check that the path organization equals the bound tenant. The framework already does this and returns 403. A failing test showed it (I had expected 404), and I removed my guard.
- **Stale smoke test:** `tests/test_smoke.py` still called the sample's old `/items` route and returned 404. The fast suite caught it and I pointed it at the new route.
- **Stale generated file:** `ruff format` rewrote a file after I had run `sandloom generate`, so `.sandloom/inspection.json` went stale. The "generated files" gate caught it, and re-running `generate` after formatting fixed it.
- **Tool slips:** I tried `timeout`, which isn't on macOS, and a foreground `sleep`, which the harness blocks. I used background runs instead.

## 4. Stuck or guessed

- **Edit and delete rights:** I assumed any member can edit or delete any note in their organization, since the notes are shared. The brief doesn't say, so author-only or admin-only edits would be a small change.
- **Migration:** I rewrote the baseline by hand instead of using `sandloom migrate new`, since nothing is deployed yet. The migration-drift gate and the migration test both pass.
- **Test fakes:** the paid gate is only tested with `fake_access_fact("organization.subscription.active", …)`. I haven't seen it against real billing data.
- **Slow commands:** `uv sync` and `sandloom test` took longer than the 2-minute foreground limit, so I ran them in the background.
