I built the `notes` module in a new Sandloom app, `teamhub`. `sandloom test` passes, with every gate green. I ran the default test run, not `--full`.

## 1. What I built
- **App:** `teamhub` was created with `sandloom new teamhub --with identity,tenancy,billing --module notes --no-git -y`. The scaffold's sample module was already called `notes`, so I replaced its sample `item` example with real notes.
- **Data:** a `Note` table with title, body, author, `created_at` and `updated_at`. Each note belongs to one organization, which is the request's tenant, and the framework filters on it. I generated a fresh baseline migration.
- **Routes** under `/api/v1/notes/notes`: create (201), list (newest first, limit capped at 100), read, edit (PATCH, partial) and delete (204).
- **Access:** every route requires membership of the organization. Create also requires an active paid subscription for that organization (402 if not). Reading, editing and deleting stay free. Notes from another organization return 404.
- **Event:** creating a note publishes `notes.note.created.v1`, carrying only `note_id`, `organization_id` and `author_id`. Nothing else consumes it yet.
- **Tests:** they cover the full create, read, edit, delete cycle and the event, list order and cap, input validation, and the cross-organization 404s. They also cover non-members getting 403, the paid gate on create with free reads after a lapse, and 401 without a login. I updated `tests/test_smoke.py` to the new route.

## 2. What I read
- `sandloom new --help`
- `AGENTS.md` and `teamhub/modules/AGENTS.md`
- `sandloom docs recipes`, then the `org-gate` and `list-order` recipes
- The scaffolded `notes` module: models, services, api, events, tests and migration
- The generated `tenancy_membership_v1` stub

## 3. Mistakes and what caught them
- **Wrong test helper assumption:** I used `as_user(...)` as if it returned an object with `.user_id`, but it returns a plain UUID. The test run caught it with `AttributeError`.
- **Stale smoke test:** `tests/test_smoke.py` still posted to the removed `/items` route. The suite caught it with a 404 where it expected 401.
- **Formatting:** ruff reported two lint errors and a format failure on my files. `ruff --fix` and `ruff format` cleared them.

## 4. Where I guessed
- **Event name:** the request only said "announce it", so I chose the channel name `notes.note.created.v1` and its fields.
- **Who may edit and delete:** you didn't say, so any member of the organization can edit or delete any of its notes, not just the author. Tell me if you want author-only or admin-only.
- **Gate on edit and delete:** I did not put the paid gate on edit or delete, reading "creating is paid, reading stays free" literally. Say so if you want edits gated too.
- **Migration:** I replaced the scaffold's migration with a new one made by `sandloom migrate new` rather than adding a second revision on top, since nothing had been deployed.
- **Billing stub:** the billing contract stub (`billing_entitlements_v1`) only appeared after `sandloom generate`. I wrote the import first, as `AGENTS.md` says it works.
