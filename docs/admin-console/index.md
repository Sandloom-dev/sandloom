---
title: Admin Console
description: A ready-made web console for the platform owner, served by your application at /admin/. No Node or separate image.
type: concept
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
uvx --from "sandloom[standard]==0.7.0a11" sandloom new myapp --with admin,tenancy,billing
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

Run this in the application's directory and type the password twice when asked:

```bash
uv run sandloom admin create --email you@example.com
```

Open `/admin/` on the address the app answers on and sign in: with the commands above
that is [http://127.0.0.1:8000/admin/](http://127.0.0.1:8000/admin/); `sandloom dev run`
prints its own address. The address counts as verified, so you don't need a mail server
yet. The command is recorded in the audit log, and so is the fact that it set a
password (`identity.user.password_set`; never the password itself).

If no one can type the password, for example an agent or a CI job runs the setup,
ask for a link instead:

```bash
uv run sandloom admin create --email you@example.com --invite --if-missing
```

The command prints a link to the console's set-password page. Whoever opens it sets
the password and is signed in. The link works once and only for 15 minutes, so it is
safe to pass on in a chat. `--if-missing` makes a second run do nothing when the
address is already an administrator.

To set the password from a secret manager, pipe it in with `--password-stdin`. There
is no flag that takes the password itself, so it never lands in your shell history.

| Situation | Command |
| --- | --- |
| Forgot which address is an administrator | `uv run sandloom admin list` |
| Forgot the password, or the link expired | `uv run sandloom admin invite --email you@example.com` |
| A verified user should become an administrator | `uv run sandloom security grant-admin --email them@example.com --apply --reason "..."` |
| Someone else registered your address first | `uv run sandloom admin create --email you@example.com --invite --take-over` |

`--take-over` makes that account yours. It ends its password, two-factor setup and
sessions. `grant-admin` refuses an unverified address for that reason: whoever
registered it first would keep their password.

While no administrator exists, the sign-in page and the application's startup log
show the `admin create` command.

Outside production, a failed sign-in names two more commands. `admin list` shows
which addresses are administrators and which of them have a password; `admin invite`
gives one a new password. In production `admin list` needs `--confirm-production
--reason "..."`; it then writes an audit record and masks the addresses unless you
add `--show-emails`. `admin invite` in production needs `--apply --confirm-production
--reason "..."`.

Whoever can run the CLI on a server can read the database anyway: the command grants
no new access. What it adds in production is an audit trail, and addresses kept out
of shell history and CI logs.

## Sessions

The console keeps its tokens in the tab's memory and never in cookies or
localStorage. In production, reloading or closing the tab signs you out.

Outside production the console also keeps the session in the tab's sessionStorage:
a reload keeps you signed in, and closing the tab still signs you out. The trade-off
is that a script injected into the page could read the stored refresh token, which
memory alone hides. That risk is fine for a development app on your own machine and
never for production. The server tells the console its mode (`/admin/config.json`),
and production never stores the session.

With the console off, as in production by default, the link opens your own frontend
instead: `FRONTEND_URL/auth/reset-password#token=...`. The token is in the URL
fragment, which the browser never sends to a server: no access log records it.
Your reset page must read the token from `location.hash`, send it in the body of
`POST /auth/reset-password`, and then remove it from the address bar with
`history.replaceState`. Identity's own reset emails still put the token in the query
string (`?token=`). A page that serves both reads either.

## Add it to an existing application

```bash
uv add "sandloom[admin]"
```

Add `"admin"` to `BUNDLED` in `asgi.py`, add `"identity"` if it isn't there, then run
`uv run sandloom generate` and restart the application.

Operators sign in through the token issuer your `claim_authorities` map assigns (the
`"issuer"` entry). That module declares the route of its login endpoint on its
registration, `define_module(..., issues_tokens=True, login_entry="/auth/login")`, and
`sandloom generate` writes both into the manifest. When the map assigns no issuer, or
the issuer declares no `login_entry`, the console shows a `503` that names the missing
piece. It never picks an issuer from the `iss` of a token.

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
