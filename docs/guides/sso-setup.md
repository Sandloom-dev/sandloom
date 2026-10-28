---
title: SSO Setup
---

# SSO Setup

The identity module supports OAuth2 / OIDC single sign-on with Google,
Microsoft, GitHub, and LinkedIn (a provider appears in
`GET /auth/sso/providers` only when its credentials are set). Telegram is
not supported yet: even with `TELEGRAM_BOT_TOKEN` set it is not listed and
`GET /auth/sso/telegram/redirect` answers `400`. This guide covers
provider registration and the client flow; see the
[Configuration Reference](../reference/configuration.md) for every SSO setting.

## Provider registration

The default redirect URI the backend sends to the provider is
`{FRONTEND_URL}/auth/sso/{provider}/callback` — register exactly that (or every
custom `redirect_uri` you pass, see below).

### Google

1. In the [Google Cloud Console](https://console.cloud.google.com/) go to
   **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client IDs**
   (configure the consent screen if prompted), type **Web application**.
2. Add the authorized redirect URIs, e.g.
   `http://localhost:3000/auth/sso/google/callback` and
   `https://yourdomain.com/auth/sso/google/callback`.
3. `.env`: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.

### Microsoft

1. In the [Azure Portal](https://portal.azure.com/): **Microsoft Entra ID → App
   registrations → New registration**; set the redirect URI
   (`.../auth/sso/microsoft/callback`).
2. Under **Token configuration → Add optional claim → ID**, add `xms_edov`
   (email domain owner verified). Without it Microsoft sign-in still works for
   accounts already linked through the authenticated link flow, but never
   auto-links or creates an account by email.
3. `.env`: `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`.

The app uses the `/common` endpoint (any tenant), so the identity key is the
immutable `tid:oid` pair from the id_token — `mail` / `userPrincipalName` are
never trusted for linking.

### GitHub

OAuth App with callback `.../auth/sso/github/callback`; `.env`:
`GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`. Only the account's **verified primary**
email counts as verified.

### LinkedIn

"Sign In with LinkedIn using OpenID Connect" product, callback
`.../auth/sso/linkedin/callback`; `.env`: `LINKEDIN_CLIENT_ID`,
`LINKEDIN_CLIENT_SECRET`.

## Allowed redirect URIs

`GET /auth/sso/{provider}/redirect?redirect_uri=...` accepts a custom redirect
URI only if it is listed in `SSO_ALLOWED_REDIRECT_URIS` (comma-separated);
anything else is `400`. Set it to your real callback URLs in production.

## Client flow

1. `GET /auth/sso/{provider}/redirect` → `{"auth_url", "state", "binding"}`
   (JSON, not an HTTP redirect). The `binding` secret is also set as the
   HttpOnly cookie `sandloom_sso_binding` (path `/auth/sso`). Navigate the browser to
   `auth_url`.
2. The provider redirects to your frontend callback with `code` and `state`.
3. Call `GET /auth/sso/{provider}/callback?code=...&state=...` with the binding:
   the cookie (same-site browser flow) or the `X-SSO-Binding` header (SPA / API
   client). Without the matching binding the state is rejected (`400`).
4. The response is a `TokenPair`, or — for a 2FA-enabled account —
   `{"mfa_required": true, "mfa_token": "...", "expires_in": 300}`; finish with
   `POST /auth/sso/mfa` `{"mfa_token", "otp"}` → `TokenPair`.

Linking a provider to the signed-in account:
`GET /auth/sso/{provider}/link/redirect` (authenticated) → same shape, state bound
to the current user → `POST /auth/sso/{provider}/link` with `code`, `state`,
`current_password` and the binding → `204`. The final link and unlink operations
require the current password for password accounts. Passwordless accounts need
a primary sign-in from the last five minutes (`auth_time`; refreshing a token
does not renew this proof). Never put a password in the redirect query string.

Unlink with `DELETE /auth/sso/{provider}/unlink`, supplying
`{"current_password": "..."}` as its JSON body where applicable; list with
`GET /auth/sso/me/linked`. Link/unlink are rate limited and repeated failed
step-up passwords trigger a per-user lockout. The account owner receives a
security notice after an explicit link commits.

## Security model

- **State** is random, stored in the cache for 10 minutes, bound to the
  initiating client (only the binding's hash is stored) and, for link flows, to
  the user; it is single-use (atomic claim). It carries the provider, purpose,
  validated `redirect_uri`, the PKCE `code_verifier` (Google, GitHub, Microsoft)
  and the OIDC `nonce` (Microsoft).
- **Account linking by email requires two verifications**: an SSO identity is
  auto-linked to an existing local account only when the provider asserts the
  email is verified **and** the local account is verified. An unverified
  provider email → `400`; an unverified local account → `409` (verify it, reset
  the password, or link explicitly). A new account is created only from a
  provider-verified email.
- SSO honours the account's second factor and `is_active` in every branch.

## Manual test

```bash
uv run uvicorn sandloom.main:app --reload
curl http://localhost:8000/auth/sso/google/redirect   # → {"auth_url": ..., "state": ..., "binding": ...}
```

Automated coverage: `uv run pytest src/sandloom_identity/tests/api/test_sso.py src/sandloom_identity/tests/api/test_sso_hardening.py -q`.
