The `banner` module is built and `uv run sandloom test banner` passes: 7 banner tests, 13 in the fast suite, lint, mypy, `sandloom check`, snapshots and migration drift. I did not run `--full`, and I didn't commit anything.

## 1. What I built
- **App:** `teamhub` created with `sandloom new teamhub --with identity,tenancy,billing --no-git --agents claude -y`. The `banner` module is in `teamhub/modules/banner/`.
- **Storage:** one `banner_banners` row per organization, tenant-scoped, with a unique constraint on the tenant so two racing first writes can't create two rows. It holds `text` (500 characters max, null once cleared), `updated_by` and `updated_at`. A clear keeps the row and sets the text to null, so the last change is still attributed.
- **Routes:**
  - `GET /api/v1/banner` is open to any member.
  - `PUT` and `DELETE /api/v1/banner` need an admin or owner. Members get 403 and anonymous callers get 401.
  - Blank or whitespace-only text, and text over 500 characters, return 422. Surrounding whitespace is trimmed.
- **Who and when:** `updated_by` and `updated_at` are returned on every read. Each set or clear also writes an audit record (`banner.set` / `banner.clear`). My tests don't check the audit records, only the row fields.
- **Isolation:** the banner is read from, and written to, the caller's own organization only. My tests cover two organizations and an admin of one sending the other's `X-Tenant-Id` header.
- **Migration:** I hand-wrote the baseline migration. The drift check confirms it matches the models.

## 2. What I read
- `teamhub/AGENTS.md` and `teamhub/modules/AGENTS.md`.
- `sandloom new --help`.
- `sandloom docs recipes` and the `org-gate` recipe.
- `sandloom docs` for `OrgRole`, `record_audit`, `authorize_resource`, `WallClock`, `UtcDateTime` and `as_user`.
- The scaffolded `example` module (api, models, services, schemas, tests, migration) as the pattern to copy.

## 3. Mistakes and what caught them
- **Migration mismatch:** my hand-written migration created an index that the model dropped when I added the unique constraint. The test run flagged it as a "removed index", and I deleted it from the migration.
- **`record_audit` is synchronous:** I called it with `await`. mypy flagged it and the tests returned 500 (`NoneType` can't be awaited). Removing the `await` fixed it.
- **macOS `sed`:** `sed -i` needs an empty suffix argument on macOS, so my first edit failed. I reran it with `-i ''`.
- **Waiting on the test run:** I tried to `sleep` while waiting, which the harness blocked, and an early placeholder monitor was wasted.

## 4. Where I guessed
- **Route design:** I put the organization in the caller's token rather than in the URL (`/banner`, not `/orgs/{org_id}/banner`). That way a path id can't disagree with the tenant the rows are filtered by. The role check reads the organization from the `X-Tenant-Id` header, else the bound tenant, and I didn't verify how the framework picks between them. My spoof test only accepts 400 or 403 for that case, and the framework turned it away.
- **Clearing:** I assumed "clear" should keep the row for attribution rather than delete it.
- **Role names:** I assumed `member`, `admin` and `owner`, taken from the recipe's role ranks.
- **Placeholder files:** I emptied the scaffold's sample `ItemCreated` event and deleted its generated schema file, since the banner has no events.
