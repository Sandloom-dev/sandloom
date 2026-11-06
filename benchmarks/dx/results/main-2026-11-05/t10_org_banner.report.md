The `banner` module is built. A full `sandloom test` run passes: the tests, ruff, mypy, `sandloom check`, the snapshots and the migration-drift check.

## 1. What I built
I created `teamhub` with `sandloom new teamhub --with identity,tenancy,billing`, then added `teamhub/modules/banner/` with `sandloom add module banner`.

- **Routes:** `/api/v1/banner/orgs/{org_id}/banner`, with `GET`, `PUT {text}` and `DELETE`.
  - `GET` needs at least the `member` role.
  - `PUT` and `DELETE` need at least `admin`.
  - Text is trimmed and must be 1–280 characters.
- **Table:** `banner_banners` (`OrgBanner`, `TenantScoped`, with a migration that enables row-level security). It holds one row per organization (unique `tenant_id`). Clearing a banner keeps the row with `text` set to null, so `updated_by` and `updated_at` still show who cleared it and when.
- **Audit:** each set or clear records `banner.set` or `banner.cleared` with the actor. The banner text is not put in the audit data.
- **Cross-organization protection:** the role check looks at the organization in the URL, but the data is scoped to the organization the request token is bound to. An admin of A who is also an admin of B could otherwise send a request with B's id in the URL and write into A's rows. So each handler refuses with 403 when the URL's organization differs from the token's.
- **Tests:** six tests in `banner/tests/test_banner.py`. They cover:
  - set, read, replace and clear, with `updated_by` and `updated_at`;
  - a member getting 403 on `PUT` and `DELETE`;
  - a non-member getting 403 on `GET`;
  - two organizations never seeing or changing each other's banner;
  - an admin of A refused on B's URL, even when also an admin of B;
  - validation (422) and unauthenticated requests (401).

  I left the `example` module in place.

## 2. What I read
- `sandloom new --help` and the generated `teamhub/AGENTS.md`.
- `teamhub/modules/AGENTS.md`, the module contract.
- `sandloom docs recipes` and the `org-gate` recipe, which gave the test pattern with the faked membership contract.
- `sandloom docs` for `OrgRole`, `record_audit`, `CurrentUser` and `sandloom.sdk.tenant`.
- The `example` module's code and tests.
- The `UserContext` source.

## 3. Mistakes and what caught them
- **Migration drift (SANDLOOM-T006):** my migration created a tenant index that the model doesn't declare. `sandloom test` caught it, and I removed the index.
- **Bulk insert refused:** I first wrote the upsert as a bulk `INSERT ... ON CONFLICT`. The tenant-safety runtime refused it with an `UnsafeTenantOperationError` in the tests. I switched to a locked ORM select, then add or update.
- **mypy:** the `clock_timestamp()` result had the wrong type, so I added a type ignore on that line.
- **Stale snapshots:** after changes, `sandloom generate` had to be re-run.
- **Wasted effort:** I ran `find /` by mistake, which timed out in the background. I also tried a `sleep` that the harness blocked.

## 4. Where I got stuck or had to guess
- **Where the organization comes from:** the docs don't say how a role check on the URL's organization relates to the token's bound tenant. I guessed the mismatch risk was real and added the 403 comparison using `current_tenant_id`, an advanced name. I didn't find a documented beginner way to do this.
- **Concurrent first write:** if two admins set a banner for the first time at once, the unique constraint makes one fail with an error instead of retrying.
- **Missing docs:** `sandloom docs authorization` doesn't exist, even though the module contract points at docs that I couldn't find locally.
- **Design choices I made without being told:**
  - `GET` and `PUT` are both organization-scoped.
  - The 280-character limit.
  - Clearing keeps the row rather than deleting it.

I did not run the app or exercise the routes with the real tenancy module. The tests fake the membership contract.
