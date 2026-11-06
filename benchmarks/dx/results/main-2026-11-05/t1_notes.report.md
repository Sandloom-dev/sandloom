The `notes` module is built in `teamhub/` and `uv run sandloom test` passes. That run covers the tests, ruff, mypy, `sandloom check`, the snapshots and migration drift.

## 1. What I built
I scaffolded `teamhub` with `sandloom new teamhub --with identity,tenancy,billing --module notes`. The scaffold already included a sample `notes` module, so I replaced its example item with real notes.

- **Table:** `notes_notes` holds title, body and author, scoped by organization (`TenantScoped`), with a migration that turns on row-level security.
- **Routes:** `/api/v1/notes/orgs/{org_id}/notes`, with create, list (newest first, capped at 100), read, `PATCH` and `DELETE`.
- **Access:** every route requires membership of the organization in the path. Only creating a note also requires an active organization subscription, so an unpaid organization gets a 402 on create. Reading, editing and deleting stay free.
- **Isolation:** every query also filters on the path's organization, so a note can't be reached through another organization even if the tenant header says otherwise.
- **Event:** creating a note publishes `NoteCreated` on `notes.note.created.v1`. It carries only ids (note, organization, author), no title or body.
- **Tests:** they cover the full CRUD flow, the event, the 402 on create for an unpaid organization with reads and edits still working, and 403 for non-members. They also cover cross-organization 404s, including a member of two organizations, plus 401 without a login and input validation.

## 2. What I read
- `sandloom new --help`
- `AGENTS.md` and `teamhub/modules/AGENTS.md`
- `sandloom docs recipe org-gate`
- `sandloom docs OrgRole` and `sandloom docs Subscribed`
- The scaffolded sample module's files and its tests
- `tests/test_smoke.py`

## 3. Mistakes and what caught them
- **Stray background command:** a bad `find` / `ls` command timed out and went to the background. I noticed it from the timeout message and left it alone.
- **`timeout`:** I called `timeout`, which doesn't exist on macOS. The shell error showed it.
- **Leftover event schema:** the old `notes.item.created.v1.json` stayed behind after I replaced the event. I only spotted it when I listed the events directory, and I deleted it by hand.
- **Lint failures:** two lines were too long and the formatting was off. `ruff` in `sandloom test` flagged them and I fixed them with `ruff --fix` and `ruff format`.
- **Smoke test:** `tests/test_smoke.py` still posted to the old `/items` route and got a 404 instead of a 401. The suite failed on it, and I pointed it at the new route.
- **BSD `sed`:** my first fix for the smoke test failed on macOS `sed`, so I used the edit tool instead.

## 4. Where I guessed
- I assumed the route shape `/orgs/{org_id}/notes`. I got it from the `org-gate` recipe rather than from a rule in the docs.
- I assumed any organization member may edit and delete any note. You didn't specify this; restricting it to the author or to admins would be a small change.
- I never read the authorization docs page itself, because `sandloom docs authorization` isn't a valid name. I relied on the docstrings and the recipe, and I wrote the tenant-header mismatch test myself.
- The cross-tenant protection comes from the explicit `tenant_id == org_id` filter. I didn't check what the framework's own tenant binding does when the header and the path disagree.
- I didn't run the app or the migrations against a real database. The tests use faked membership and subscription checks, as the recipe shows.
