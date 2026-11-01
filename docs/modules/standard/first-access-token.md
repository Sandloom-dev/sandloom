---
title: Get your first access token
description: Register, verify your email in a local inbox, sign in and refresh organization claims.
---

# Get your first access token

The identity battery requires a verified email before password login. The
default `log` notifier records delivery metadata, but deliberately keeps email
addresses, message bodies and verification tokens out of logs. Use a local SMTP
inbox to complete the flow; no external email account is needed.

## Start a local inbox

Create an application with identity and tenancy:

```bash
uvx --from "sandloom[standard]==0.7.0a5" sandloom new myapp --with identity,tenancy --module notes
```

Follow the printed quickstart through `uv sync`, `uv run sandloom generate`,
starting the development services and applying migrations. Before starting the
API, launch [Mailpit's official Docker image](https://mailpit.axllent.org/docs/install/docker/):

```bash
docker run --rm -d --name sandloom-dev-mail \
  -p 127.0.0.1:1025:1025 -p 127.0.0.1:8025:8025 \
  axllent/mailpit:v1.32.0
```

Add these values to the application's `.env`:

```dotenv
NOTIFIER_BACKEND=smtp
SMTP_HOST=127.0.0.1
SMTP_PORT=1025
SMTP_FROM=no-reply@example.com
SMTP_USE_TLS=false
```

Leave `SMTP_USERNAME` and `SMTP_PASSWORD` unset for this local inbox. These host
settings apply when the API runs with `uv run uvicorn myapp.asgi:app --reload` on
your computer. A containerized API needs the inbox's reachable service hostname
instead. The inbox UI is [http://127.0.0.1:8025](http://127.0.0.1:8025).

This configuration captures development mail on loopback only. Production uses
your real SMTP provider and verified STARTTLS (`SMTP_USE_TLS=true`); Sandloom
rejects SMTP credentials sent without TLS in production.

## Register and verify

Start the API and open its interactive API documentation at
[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

1. Execute **POST `/auth/register`** with a new address:

    ```json
    {"email": "alice@example.com", "full_name": "Alice", "password": "Example-Passw0rd!"}
    ```

    The response is `202`; registration deliberately returns the same response
    for an existing address. Login before verification returns `403`.

2. Open the message in the local inbox. Copy the value after `token=` from its
    verification URL.

3. Execute **POST `/auth/verify-email`**:

    ```json
    {"token": "paste-the-verification-token-here"}
    ```

    Success is `204`. Sandloom provides the verification API; the URL in the
    email points to your frontend's verification page. A backend-only application
    can use this API request directly without implementing that page yet.

If registration happened before SMTP was configured, execute
**POST `/auth/resend-verification`** with `{"email": "alice@example.com"}` after
restarting the API. It returns `204` and sends a fresh link for an unverified
account.

## Sign in

Execute **POST `/auth/login`** with the same address and password:

```json
{"email": "alice@example.com", "password": "Example-Passw0rd!"}
```

The `200` response contains `access_token`, `refresh_token`, `token_type` and
`expires_in`. Keep the refresh token locally. In Swagger UI, click **Authorize**
and paste the access token into the bearer-token field. **GET `/auth/me`** should
now return your account. Application module routes authenticate the same way.

## Refresh after creating an organization

With the access token authorized, execute **POST `/api/v1/orgs`**:

```json
{"name": "My workspace", "slug": "my-workspace"}
```

The response is `201` and contains the organization's `id`. The creator becomes
its owner. The access token you already hold was minted before this membership
existed, so its `tenant_ids` claim does not include the new organization yet.

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

The new organization's item list returns `200` with `[]`. Using the old access
token with that `X-Tenant-Id` returns `403`; this does not mean organization
creation failed. Accepting an invitation requires the same refresh. Refresh
tokens rotate, so subsequent refreshes use the newest one.

Stop the inbox when finished:

```bash
docker stop sandloom-dev-mail
```

Next: [tenant boundaries and membership rules](tenancy.md),
[identity's security behavior](identity.md) and
[authentication and authorization](../http/authorization.md).
