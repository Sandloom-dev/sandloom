I built the `notes` module in a new Sandloom app, `teamhub`, and `uv run sandloom test` passes on everything: tests, ruff, mypy, `sandloom check`, the generated-file and snapshot checks, and migration drift.

**1. What I built**
- **App:** `sandloom new teamhub` with the bundled identity, tenancy and billing modules.
- **Routes:** `teamhub/modules/notes/` serves `/api/v1/notes/orgs/{org_id}/notes`, with POST, GET list (newest first, limit capped at 100), GET one, PATCH (partial edit of title and/or body) and DELETE.
- **Table:** `notes_notes` holds id, title, body, author_id and created_at, with row-level isolation per organization. I rewrote the module's baseline migration by hand for it.
- **Event:** creating a note publishes `notes.note.created.v1`, carrying the note id, organization id and author id. Nothing in the app listens to it yet.
- **Access rules:** every route requires membership of the organization in the path. Creating a note also requires an active subscription on that organization, and it returns 402 when unpaid. Reading, editing and deleting stay free.
- **Cross-organization access:** a member of A asking for B's path gets 403, and so does a member of both organizations whose token is bound to A. Inside their own organization, another organization's notes return 404 on get, edit and delete, and an empty list.
- **Tests:** 7 tests in `tests/test_notes.py` cover the full lifecycle, ordering and the limit cap, cross-organization isolation, the paid gate, non-members and anonymous callers, and validation.
- **Leftover:** the scaffold's `example` module is still in the app; I didn't remove it.

**2. What I read**
- `sandloom new --help`
- `teamhub/AGENTS.md`
- `teamhub/modules/AGENTS.md`
- the `sandloom docs recipe org-gate` recipe
- `sandloom docs` for `OrgRole`, `SubscriptionActive`, `as_user`, `events` and the `authz` and `tenant` reference pages
- `sandloom context notes`
- the code the scaffold generated for the `notes` module

**3. Mistakes and what caught them**
- `sandloom new` printed a "generated files not written" note because `celery` was missing before `uv sync`. Running `uv sync` and then `sandloom generate` fixed it.
- My first full run failed `ruff format`, and the generated files then went stale. Running `ruff format` and `sandloom generate` fixed both.
- I assumed a member of two organizations, bound to A and asking for B's path, would get an empty list. The framework actually returns 403 `tenant_mismatch`, and one test failed on it. I changed the test to expect the 403.
- I called `Monitor` before its schema was loaded, and a `sleep` wait was blocked. I read the background task's output file instead.

**4. Where I got stuck or guessed**
- **Docs:** the docs paths the guide names (`docs/modules/http/authorization.md` and the rest) aren't in the installed package. I searched the filesystem for them, but that search timed out. I used the CLI docs and the recipe instead.
- **Roles:** I guessed that "member of the organization" means the `member` role and above. Editing and deleting aren't limited to a note's author, because the request said only that members can edit and delete.
- **Paid gate:** I chose the live `SubscriptionActive` check over the cached `Subscribed` claim, so a new subscription takes effect immediately.
- **Tests:** they fake the membership contract and the subscription fact, as the recipe does, so they don't exercise the real tenancy and billing modules end to end.
