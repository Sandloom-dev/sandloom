---
title: Get your first access token
description: Call your own route in the first hour - print a development token with one command, or register and verify with the printed link.
type: how-to
---

# Get your first access token

Two ways to a bearer token for your own routes, both without an email account
or a mail server:

- **`sandloom dev token`.** One command prints a token the app accepts, for
  development and tests of your routes. It never runs in production.
- **The real flow.** Register, verify the email with the link the app prints,
  sign in, refresh after creating an organization. It is the flow your users
  take, so do it once.

## Create the application

```bash
uvx --from "sandloom[standard]==0.7.0a17" sandloom new myapp --with identity,tenancy --module notes
```

The `tenant_not_authorized` code, the tenant error texts and the printed
`[dev only] no frontend?` step below need `0.7.0a16` or later. With `0.7.0a15`
the same answers carry no `code` and the older texts.

Follow the printed quickstart through `uv sync`, `uv run sandloom generate`,
`uv run sandloom dev up` and the migrations. The generated `.env` already holds
`NOTIFIER_LOG_LINKS=true` for an application with identity: the running API
prints each email's links, marked `[dev only]`. Production refuses that
setting. When you want a real inbox, configure a notification driver as
[Notifications](../../infrastructure/notifications.md) describes.

## A token in one command

With the API running (`uv run sandloom dev run` in another terminal), pick any
tenant id and ask for a token that grants it:

```bash
TOKEN=$(uv run sandloom dev token --tenant 00000000-0000-4000-8000-000000000001)
curl --fail-with-body http://127.0.0.1:8000/api/v1/notes/items \
  -H "Authorization: Bearer $TOKEN"
```

That is the whole path: no registration, no organization, no email. The token
goes to standard output and nothing else does, so it fits in `$(...)`. What it
carries goes to standard error: the subject, the tenant, the roles and how long
it lives.

A tenant-scoped route of your application trusts the tenant the token grants.
It does not look for a membership. Tenancy's own organization routes
(`/api/v1/orgs/...`) check membership live. A tenant made up this way is not an
organization there, so they do not list or open it. To act in an organization
you created through the real flow below, pass its id instead of this one.

| Flag | What it does |
|---|---|
| `--tenant UUID` | grants that tenant, the way a tenancy membership would |
| `--role NAME` | carries a role; repeat it for several (at most 16) |
| `--user UUID` | the subject; the default is one fixed development user |
| `--ttl-minutes N` | how long it lives: 60 by default, 1 to 1440 |
| `--quiet` | prints the token only |

The app accepts the token as it accepts one it issued itself. The app's own key
signs it. It also carries the proof of origin that the app's claim policy asks
for ([ADR-0014](../../adr/0014-claims-trust-and-authorization-portability.md)).
Its role and tenant claims therefore count. A JWT you sign by hand has no such
proof; the app drops its roles and tenants.

For that reason the command grants a role or a tenant only when a module of
your application vouches for it, as identity and tenancy do. Asking for one that
no module vouches for is an error that names the missing assignment. It is not a
token that answers `403`.

The token has no account behind it, so a route that reads the account, such as
`/auth/me`, answers as it does for an unknown user. It carries no permissions.

### When the command refuses

It runs only on a developer machine, and it says why when it does not:

- `ENVIRONMENT` must be `dev` or `test`, written exactly so in the environment
  or in `.env`. `prod` and a typo such as `Dev` are refused. So is a missing
  value: the app runs as `dev` then, but a command that signs a credential does
  not guess.
- `DATABASE_URL` must point at this machine. A unix socket and a file database
  count as this machine. Every host the url can reach is checked: the one in its
  address and those of its `host` and `hostaddr` parameters. When a PostgreSQL
  url names no host, `PGHOST` is checked instead. A `.env` copied from production that still says
  `ENVIRONMENT=dev` is refused for its remote database. No flag overrides
  this. The check covers `DATABASE_URL` only, not the urls under `INFRA`.
- `CI` must not be set to a true value (`1`, `true`, `yes` or `on`). A test mints
  its own tokens with `as_user` ([testing](../../reference/testing.md)).

Those checks do not protect a key. Every token the command mints carries a
`dev_mint` marker. An app refuses that marker unless its own `ENVIRONMENT` is
`dev` or `test`. A token minted on a machine that holds a production key
therefore answers `401` on production, whatever role and tenant it names. A
holder of a signing key can still sign anything, so keep production secrets out
of a development checkout.

The signing key is never printed. When the app's settings or its composition
root do not load, the command says so. It leaves out the settings' own error
text, which can echo the value that failed. `uv run sandloom --debug dev token`
adds the last error line of the failed process, without urls.

### An application without tenancy

`sandloom new --no-batteries` composes neither identity nor tenancy. No module
vouches for a role or a tenant, so the command refuses both flags, `--role` and
`--tenant`. With `--with identity` alone, identity vouches for roles. There
`--role` works and only `--tenant` is refused. A token without flags is a valid
login for routes that need nothing more.

A tenant-scoped route, such as the generated module's, still has to know the
tenant. With `TENANCY_MODE=multi`, the default of the settings, nothing can
bind one for any token. The route answers `400 tenant_required`. When the
request names a tenant in `X-Tenant-Id`, it answers `403 tenant_not_authorized`,
since no token lists one. The same holds for any app made without `tenancy`,
such as `--with identity` alone: a real login carries no tenant either. So the
generated `.env` of an app without the tenancy battery binds one tenant for
every request:

```dotenv
TENANCY_MODE=single
SINGLE_TENANT_ID=<a UUID generated for your app>
```

`uv run sandloom dev token` with no flag is enough, and the generated route
answers `200`. `.env.example` carries the same two lines commented out, since a
production deployment decides its own tenancy. An app made earlier sets the two
lines itself, with any UUID. For several tenants, add the tenancy battery and
set `TENANCY_MODE=multi`. Startup logs a warning when a module is the tenant
authority (`tenant_ids`) under `TENANCY_MODE=single`. The configured tenant
binds every request there, so the tenants that module grants are never used.

## The real flow

The identity battery requires a verified email before password login. In
development the app prints the links of the emails it sends instead of mailing
them. The verification link is in the terminal where `sandloom dev run` runs.

### Register and verify

Start the API with `uv run sandloom dev run` and open its interactive API
documentation at the address it prints, `http://127.0.0.1:8000/docs` when that
port was free.

1. Execute **POST `/auth/register`** with a new address:

    ```json
    {"email": "alice@example.com", "full_name": "Alice", "password": "Example-Passw0rd!"}
    ```

    The response is `202`; registration deliberately returns the same response
    for an existing address. Login before verification returns `403`.

2. Find the message in the `sandloom dev run` output. Its verification link is
    marked `[dev only]`. Copy the value after `token=`.

3. Execute **POST `/auth/verify-email`**:

    ```json
    {"token": "paste-the-verification-token-here"}
    ```

    Success is `204`. Sandloom provides the verification API; the URL in the
    email points to your frontend's verification page. A backend-only application
    can use this API request directly without implementing that page yet, and
    the output says so after the link: `[dev only] no frontend? POST
    /auth/verify-email {"token": "..."}`. A notifier that really sends mail,
    such as the `smtp` driver, prints nothing.

If registration happened before `NOTIFIER_LOG_LINKS=true` was set, set it,
restart the API and execute **POST `/auth/resend-verification`** with
`{"email": "alice@example.com"}`. It returns `204` and prints a fresh link for
an unverified account.

### Sign in

Execute **POST `/auth/login`** with the same address and password:

```json
{"email": "alice@example.com", "password": "Example-Passw0rd!"}
```

The `200` response holds the two tokens (`access_token`, `refresh_token`) with
their `token_type` and `expires_in`. Keep the refresh token locally.

In Swagger UI, click **Authorize** and paste the access token into the
bearer-token field. **GET `/auth/me`** should now return your account.
Application module routes authenticate the same way.

### Refresh after creating an organization

With the access token authorized, execute **POST `/api/v1/orgs`**:

```json
{"name": "My workspace", "slug": "my-workspace"}
```

The response is `201` and contains the organization's `id`. The creator becomes
its owner. `slug` is optional: without it, the slug is derived from the name
(`my-workspace` here). Your access token was minted before this membership
existed. Its `tenant_ids` claim does not list the new organization yet, so
refresh it as below.

Execute **POST `/auth/refresh`** with your current refresh token:

```json
{"refresh_token": "paste-the-current-refresh-token-here"}
```

Replace **both** tokens with the returned pair, then update Swagger UI's
**Authorize** value to the new access token. Tenancy's own **GET
`/api/v1/orgs/{org_id}`** checks membership live and can already return `200`
before refresh. Tenant-scoped application routes instead validate the selected
tenant against the token's claims.

For example, run the following in a terminal. Enter the **new access token** on
the first input line and the **organization ID** on the second:

```bash
read -r ACCESS_TOKEN
read -r ORG_ID
curl --fail-with-body http://127.0.0.1:8000/api/v1/notes/items \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "X-Tenant-Id: $ORG_ID"
```

The new organization's item list returns `200` with `[]`. The old access token
with that `X-Tenant-Id` returns `403 tenant_not_authorized`. Its `detail` says
to refresh the access token or log in again. This does not mean organization
creation failed. Accepting an invitation requires the same refresh. Refresh
tokens rotate, so subsequent refreshes use the newest one.

Next: [tenant boundaries and membership rules](tenancy.md),
[identity's security behavior](identity.md) and
[authentication and authorization](../http/authorization.md).
