---
title: Develop the console
description: How the console is built, how to change it from source, how sessions and command recovery behave, and how to troubleshoot it.
---

# Develop the console

You need this page only to change the console itself. It is a React and TypeScript
application in the repository's `admin/` directory, built on Radix primitives with
Tailwind v4 and TanStack Query/Table. Component structure, design tokens and the rules
that keep them consistent are in `admin/README.md`.

## How it works

The generation pipeline has four stages:

1. A business module provides its normal public HTTP API, DTOs, and domain logic.
2. Its `admin.py` explicitly admits resources, fields, operations, and visibility.
3. `sandloom generate` validates these declarations against the composition's
   OpenAPI and writes a versioned static JSON manifest.
4. The shared browser renderer loads that manifest and builds standard screens.

The exporter generates **data**, not per-resource React source files or backend
CRUD handlers. The browser needs no runtime ORM access and no production OpenAPI
endpoint. A module's permissions, UoW, RLS, audit, and events remain in its API.
Unsupported declarations fail export; the UI does not guess from the first record.

The bundled UI and module-owned labels are English. Use English labels,
confirmation text, and descriptions in new declarations; user-entered data is not
translated. Dates and numbers use `en-US` formatting. Operator help is under
**Operator guide** in the navigation; ⌘K (Ctrl+K) opens the command palette.

## Local development

With a Sandloom backend running, install Node >=22.12 and run:

```bash
cd admin
npm ci
SANDLOOM_ADMIN_API_TARGET=http://127.0.0.1:2222 npm run dev
```

Open `http://127.0.0.1:5173` and sign in with an existing platform administrator
account. Password login supports an optional 2FA code. The platform role grants
explicit global operator APIs; it does not create tenant membership.

For a stable local operator preview, run `npm run build` followed by
`SANDLOOM_ADMIN_API_TARGET=http://127.0.0.1:2222 npm run preview -- --port 5187 --strictPort`.
Use the development server for editing only. After changing dependencies, stop it,
restart with `npm run dev -- --force`, and reload tabs to discard stale pre-bundles.
The browser test harness builds and serves the static app rather than testing a
long-lived hot-reload session.

## Package the console

The `sandloom-admin` wheel carries the built console in
`src/sandloom_admin/console`. After changing anything under `admin/`:

```bash
cd admin
npm run package
```

Commit the result. A test compares its recorded hash with the source, so a stale copy
fails `sandloom test`. Python contributors and the release build need no Node.

To see it end to end, from real wheels through a browser sign-in:

```bash
uv run python tools/smoke/admin_package_check.py \
  --postgres-url postgresql://user:password@127.0.0.1:5433 \
  --redis-url redis://127.0.0.1:6380
```

## Run it as a separate container

`npm run build` produces static files for a root path, and the
`deploy/docker-compose.admin.yml` overlay serves them from a non-root nginx image built
from this repository. This is for working on the console; applications use the package.

## Operator API and topology

The platform views are kernel routes installed once per loaded module at
`/api/v1/platform/<module>/`: `audit` (keyset: `cursor` + `X-Next-Cursor`,
`since`/`until`, `action`/`action_prefix`, `actor_id`, `resource_type`,
`resource_id`), `services`, `outbox/dead`, `streams/dlq?channel=`, `consumers`,
`projections`, `contracts`, `modules`, `settings` (platform admin, live
session check) and the commands `POST outbox/requeue` and
`POST streams/replay` (platform admin, an admin-risk write: fail-closed
`Idempotency-Key`, required audit, commit-time session re-check; the same store/transport operations as the `sandloom outbox` / `sandloom streams`
CLIs). In a split deployment the gateway routes each prefix to the service
hosting that module, so each view reports on that process and database.

Monetary refunds, invoices and provider subscription changes open the independently
authenticated Stripe dashboard. No local row edit simulates a PSP operation.
The [operator guide](operations.md) documents limitations and retry handling.

Tenancy owns `/api/v1/platform/tenancy/organizations` and `/members`; billing owns
`/api/v1/platform/billing/*`. They use the public shared platform-admin guard:
identity must be a successful claims source and the subject must hold `admin`.
Every read checks the session live. Catalog changes carry the admin risk and
credit adjustments the money risk; both re-check the session before commit. Services query only
their own tables; the UI follows UUID references instead of runtime SQL joins.

The kernel adds `/api/v1/platform/<loaded-module>/audit` and `/services`. Audit
reads the module descriptor's hosting database through the optional
`AuditStore.query` contract; the SQLAlchemy implementation contains the relational
query and explicitly allowlists metadata. Unsupported drivers return `501`.
Health exposes statuses only through a separately authenticated owner surface.
Existing internal diagnostics remain internal. The split gateway routes each
platform prefix to its owner, so the static UI needs no topology-specific logic.

A shared database yields the same audit journal under multiple
source views. Health covers that API process and configured peers, not workers
or the whole container fleet.

## Sessions and verification

Tokens are kept only in memory. Reloading the tab requires login. Refresh is
single-flight; a revoked session requires new authentication. Session changes
cancel requests and clear cached data, while organization queries include the
user and organization in their keys. Security mutations wait for the server
and are not automatically replayed after uncertain network failures.

Run `npm test`, `npm run test:browser`, `npm run build`, `npm run audit` and
`npm run contracts:check` from `admin/`. The contract check needs the backend
virtualenv installed and checks generated types without exposing production
OpenAPI. Browser tests use synthetic API fixtures; backend authorization is
verified separately by backend tests. A browser acceptance test adds an extra module solely through its compiled
manifest, starting with zero records, then creates a record and executes a command;
another switches tenant contexts for a custom scoped resource. Owner-console tests cover tenant-account rejection, global directories, service health, and a refused credit adjustment with a stable operation reference. The fixture is compiled from a Python test API
and checked for drift. The Docker smoke tests two configured upstreams and does
not claim a full live production deployment.

See [authorization](../modules/http/authorization.md),
[multi-tenancy](../modules/database/multi-tenancy.md),
[tenancy API](../modules/standard/tenancy.md), and the
[operator guide](operations.md). Internal design research is not part of
the public distribution.

## Command recovery and scoped resources

Each command freezes its payload, target/scope and operation reference before
sending. Unknown outcomes disable the original fields and retain that exact
attempt in the current session, including across dialog closure and page
navigation. The recovery banner identifies the pending operation and can retry
it. Other writes wait until it is resolved; reads remain available. Session
changes discard recovery state. Reloading warns, but cannot persist authority
or operation bodies: inspect server state before creating another operation.

Bulk commands remember confirmed per-record successes and do not resend them.
Definite refusals remain selected and show their individual errors; an unknown
outcome retries the original selection with the original per-record keys.
Every confirmed write invalidates audit queries as well as its declared data.

Tenant-scoped list/detail/create/edit/cancel/save/delete links retain `?tenant=`.
Changing scope remounts local form/selection state; another tenant's rows are
never used as query placeholder data. Server authorization remains independent.

Record Activity uses the declared audit type and ID, with links to the complete
filtered explorer. Outbox/DLQ inspection follows `X-Next-Cursor`; repairing a
selection returns to the first page. The outbox badge is a visible-window lower
bound (`100+` when more exist), not an invented total backlog count.

## Updating and troubleshooting

Keep `admin.py` declarations and their public APIs in the same change. Regenerate
the manifest after changing a declaration or DTO. For the bundled platform,
`npm run contracts` updates the manifest, public OpenAPI snapshot, and generated
TypeScript types; `npm run contracts:check` fails if any drift. Applications
use `sandloom generate`, and `sandloom test` checks the result.

An ordinary resource update requires a new manifest/config, not a new React
screen. New compiled widgets or a manifest-language version require a compatible
UI build. Mount replacement JSON files read-only and reload the browser. Both
JSON responses use `Cache-Control: no-store` in the supplied nginx image.

| Failure | Check |
| --- | --- |
| Export reports an operation is not owned by a module | Bind its own `/api/v1/` endpoint; preserve endpoint module metadata in decorators. Do not point a declaration at another module's API. |
| Export reports an unsupported field/schema | Use a supported scalar DTO input or a specialized compiled component; do not silently drop required constraints. |
| UI reports invalid configuration | Use manifest version 1, unique resource/module keys, and only module names present in the exported manifest. Identity must remain enabled. |
| UI needs an unknown component | Deploy a UI build containing that widget or remove its declaration. The manifest cannot download executable components. |
| API requests fail locally | Check `SANDLOOM_ADMIN_API_TARGET` and that the backend, migrations, account, and required services are ready. |
| Requests reach the wrong split service | Check `SANDLOOM_ADMIN_API_UPSTREAM` and public gateway routing; no browser internal RPC token should be configured. |
| Many operators share a rate-limit identity | Configure the full trusted proxy chain on the backend; see `admin/README.md`. |

Local test/demo accounts are provisioned separately from the UI. Their credentials
must not be written into manifests, source files, or container images.
