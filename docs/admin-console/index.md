---
title: Admin Console
description: A ready-made web console for the platform owner, served by your application at /admin/. No Node or separate image.
---

# Admin Console

An optional web console for you and your operations team: users, organizations,
billing, audit and service health. Install one package and your application serves
it at `/admin/`. You need neither Node nor a separate image.

The console talks only to your modules' public APIs, so their permissions, audit and
transactions still apply. Organization roles alone don't grant access: it requires
the platform `admin` role.

## Try it

Create an application with the console. It brings identity, which handles sign-in:

```bash
uvx --from "sandloom[standard]==0.7.0a6" sandloom new myapp --with admin,tenancy,billing
cd myapp
uv sync
uv run sandloom generate
docker compose -f deploy/docker-compose.dev.yml up -d --wait
uv run sandloom migrate upgrade-all --apply
uv run uvicorn myapp.asgi:app --reload
```

`sandloom generate` writes `myapp/shared/admin-manifest.json`, the description of your
screens. Run it again whenever a module's routes or `admin.py` change; `sandloom test`
fails when the file is stale.

## Create the first administrator

Register an account while the application runs:

```bash
curl -X POST http://127.0.0.1:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email": "you@example.com", "full_name": "You", "password": "Example-Passw0rd!"}'
```

Then make it an administrator. The command reports what it would do until you add
`--apply`:

```bash
uv run sandloom security grant-admin --email you@example.com
uv run sandloom security grant-admin --email you@example.com --apply --reason "first administrator"
```

This also marks the address as verified, so you don't need a mail server yet. The grant
is recorded in the audit log. Open
[http://127.0.0.1:8000/admin/](http://127.0.0.1:8000/admin/) and sign in.

## Add it to an existing application

```bash
uv add "sandloom[admin]"
```

Add `"admin"` to `BUNDLED` in `asgi.py`, add `"identity"` if it isn't there, then run
`uv run sandloom generate` and restart the application.

## What's in it


- Customers: global users, organizations, memberships and billing customers.
- Access control: platform roles, assignments and permission catalog.
- Billing: subscriptions, products/prices, balances, immutable ledger, purchases,
  refunds/disputes, webhook journal and operation intents. Product display name /
  availability and confirmed credit adjustments have explicit audited commands.
- Account safety: user search, emergency block/unblock and revoke-sessions
  commands (reason required, typed confirmation for blocks, audited).
- Platform: an audit explorer (source switch, time range, action prefix,
  actor/resource filters, keyset paging, actor names), live service health,
  event delivery (outbox dead letters with requeue, Streams DLQ with targeted
  replay, consumers, projections), contracts & modules, redacted runtime
  settings. Every record page shows its audit trail as an activity timeline.

Sections for tenancy and billing appear only when your application loads those modules.
For everyday tasks and permissions, read the [operator guide](operations.md).

## Next

- [Add your module](add-your-module.md): give a module its own screens with no frontend work.
- [Deploy](deploy.md): turn it on in production and restrict access.
- [Operator guide](operations.md): what each screen does.
