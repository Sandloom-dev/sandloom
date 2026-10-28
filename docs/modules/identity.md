---
title: Identity
---

# Identity Module (`sandloom_identity`)

Identity is the **required** core module providing authentication and
authorization: user accounts, password and JWT-based login (access/refresh
tokens), email verification and password reset, RBAC (roles, permissions, and
their assignments), two-factor auth (TOTP + backup codes), and SSO / external
identity linking.

It mints access tokens and contributes role and permission claims into them.
The claims contributor is registered `critical=True` (exposed as
`identity.claims.v1`), so a claims failure aborts token minting with `503`
rather than issuing a privilege-stripped token.

## Security boundaries and recovery

Security-sensitive account changes require step-up proof. SSO link/unlink and
2FA changes use the current password for password accounts; passwordless
accounts need a primary sign-in within five minutes. Failed password proofs
share a per-user lockout. A bearer token alone cannot attach a persistent login
method.

To recover after losing an authenticator, submit an unused backup code in `otp`
to `/auth/2fa/disable` or `/auth/2fa/backup-codes/regenerate`, with the same
step-up proof. Codes are single use; a six-digit TOTP, an eight-character legacy
hex backup code or a twenty-character current hex backup code is accepted.
`/auth/2fa/verify` explicitly consumes a code; it does not mint a reusable
step-up assertion or replace the proofs on sensitive endpoints.

Refresh-token families have an absolute lifetime from their original
`auth_time`: `MAX_SESSION_DAYS` defaults to 30 (range 1–365). Successor expiry is
capped by that instant as well as `REFRESH_TOKEN_EXPIRE_DAYS`. Expired families
and legacy families without `auth_time` require a new primary sign-in.

The administrative DELETE operation is reversible account deactivation, not
GDPR erasure. An administrator can reactivate the account; both reactivation
paths record required audit entries. Cross-module erasure, retained financial
records and organization ownership transfer require a separate operator
workflow. Deactivated-account registration attempts return the same public
response and notify the owner to contact the administrator.

Identity event serialization omits the actor's IP address. The in-memory actor
can still supply it to security checks and audit handling.

## Domain entities

| Model | Table | Description |
| --- | --- | --- |
| `User` | `users` | Core user account (`password_reset_nonce` makes reset links single-use). |
| `Role` | `roles` | Named RBAC role. |
| `Permission` | `permissions` | Granular permission. |
| `RolePermission` | `role_permissions` | Role ↔ permission link. |
| `UserRole` | `user_roles` | User ↔ role assignment. |
| `RefreshToken` | `refresh_tokens` | Refresh tokens (sha256 hash only), grouped in rotation families (`family_id`). |
| `TwoFASecret` | `twofa_secrets` | TOTP secret per user (encrypted at rest). |
| `TwoFABackupCode` | `twofa_backup_codes` | 2FA recovery codes (hashed). |
| `ExternalIdentity` | `external_identities` | SSO provider ↔ user link. |

## HTTP API

| Router | Prefix | Endpoints |
| --- | --- | --- |
| Auth | `/auth` | `POST /register`, `POST /login`, `POST /refresh`, `POST /logout`, `POST /verify-email`, `POST /resend-verification`, `POST /request-password-reset`, `POST /reset-password`, `POST /change-password`, `GET /me`, `PATCH /me` |
| SSO | `/auth/sso` | `GET /providers`, `GET /{provider}/redirect`, `GET /{provider}/callback`, `POST /mfa`, `GET /{provider}/link/redirect`, `POST /{provider}/link`, `DELETE /{provider}/unlink`, `GET /me/linked` |
| 2FA | `/auth/2fa` | `GET /status`, `POST /init`, `POST /enable`, `POST /disable`, `POST /verify`, `GET /backup-codes`, `POST /backup-codes/regenerate` |
| Users | `/api/v1` | `GET /users` (admin), `GET`/`PUT /users/{user_id}` (self or admin), `DELETE /users/{user_id}` (admin, soft delete), `GET /users/{user_id}/roles` |
| Roles | `/api/v1` | role CRUD, `GET`/`POST /permissions`, attach/detach `/roles/{role_id}/permissions/{permission_id}` |
| User roles | `/api/v1` | assign / revoke `/users/{user_id}/roles/{role_id}` |
| Account safety | `/api/v1` | platform admin: `GET`/`POST /users/{user_id}/block`, `POST /users/{user_id}/unblock`, `POST /users/{user_id}/revoke-sessions`, `POST /users/{user_id}/reset-2fa` (lost second factor; ends sessions) (body `{"reason"}`, `Idempotency-Key` header required, admin risk, audited) |

### Behaviour API clients must know

- **Registration** — `POST /auth/register` always answers `202` with the same
  body (`{"message": ...}`) whether or not the email already exists (no account
  enumeration). The mailbox owner gets a verification link, or an "account
  exists" notice. `POST /auth/resend-verification` and
  `POST /auth/request-password-reset` always answer `204`.
- **Unverified accounts cannot sign in.** `POST /auth/login` answers `403` for a
  password account whose email is not verified. Completing a password reset
  also verifies the address.
- **Login errors** — wrong credentials `401`; 2FA enabled but no `otp` → `401`
  with header `X-2FA-Required: true`; inactive account `401`.
- **Lockout (two tiers, no lockout DoS)** — on `POST /auth/login`, 5 failed
  password/2FA checks within 15 minutes lock the **(account, client IP)** pair
  (exponential, 30 s up to 15 min) → `429`; an attacker failing on purpose
  locks only themself, never the owner signing in from elsewhere. Across ALL
  IPs a softer per-account throttle applies: from 20 failures in the window
  every further attempt on the account is **delayed** (0.5 s per failure above
  the threshold, capped at 5 s) — slowed, never refused. Trade-off: a
  distributed attacker gets 5 guesses per IP per window (further bounded by
  the IP rate limiter and the delay) in exchange for an account that cannot be
  locked by a third party; strong passwords and 2FA remain the real defence.
  Unknown emails are counted, locked and audited the same way
  (`identity.login.locked` with `resource_type="login_subject"` and no
  `resource_id` when the email has no account): a failed login does the same
  Redis commands, SQL statements, transaction and single argon2 pool
  submission (queued before the account lookup, against a dummy hash for an
  unknown email) whether or not the account exists, so neither the response
  nor its timing reveals it. `POST /auth/2fa/verify`, SSO MFA and
  `POST /auth/change-password` (callers that already proved a first factor)
  keep a per-account hard lock. On a cache outage the lock fails closed (`503`)
  in production and open elsewhere.
- **Refresh-token rotation with reuse detection** — every `POST /auth/refresh`
  consumes the presented token and returns a new pair in the same family.
  Presenting a token rotated **at most 10 s ago** (or losing a concurrent
  refresh race) while its family still has a live successor is a benign race
  (two tabs, a retry after a lost response): `409` "Refresh token already
  rotated; retry with the new token", nothing revoked and nothing issued.
  Beyond that window — or when the family has no live successor (logout,
  family revocation) — reuse revokes the whole family and answers `401`.
- **Reset links are single-use** — a link carries the user's current reset nonce
  (cleared on use, rotated by every new request) and dies with any password
  change.
- **`POST /auth/change-password`** — body `{current_password, new_password}`;
  revokes every refresh token and outstanding access token of the user and
  returns a fresh `TokenPair`. Password-less (SSO-only) accounts get `400` and
  must use the reset flow.
- **Step-up re-auth for 2FA changes** — `POST /auth/2fa/init`, `/enable`,
  `/disable` and `/backup-codes/regenerate` require `current_password`; an
  account without a password instead needs an access token whose `auth_time`
  claim (the primary sign-in instant) is within the last 300 s (a fresh SSO
  login). `auth_time` is carried unchanged through refresh — a refreshed
  token's fresh `iat` never counts. Otherwise `401`.
- **RBAC audit** — every role / permission mutation (create, update, delete,
  grant or revoke a permission on a role, assign / revoke a role) records a
  `required=True` audit row (`identity.role.*`, `identity.permission.created`)
  in the same UoW.
- **Session revocation** — deactivation, password reset/change, role
  revocation and revoking a permission from a role (every holder of the role)
  call `revoke_user_tokens_on_commit` (`sandloom.sdk.security`;
  it runs once the change commits — a rolled-back change revokes nobody — and
  logs a failed revocation): every framework token check
  then rejects older access tokens with `401 "Token has been revoked"`, and
  `identity.user_sessions_revoked.v1` is published.
- **SSO** — see the [SSO guide](../guides/sso-setup.md): the callback must carry
  the flow binding (cookie or `X-SSO-Binding` header); a 2FA-enabled account
  receives `{"mfa_required": true, "mfa_token": ..., "expires_in": ...}` and
  finishes with `POST /auth/sso/mfa` (an email auto-link to such an account is
  only written after the second factor succeeds); a callback with a wrong
  binding is rejected without consuming the state; auto-linking by email requires both a
  provider-verified email (`400` otherwise) and a verified local account
  (`409` otherwise — link explicitly via `GET /auth/sso/{provider}/link/redirect` +
  `POST /auth/sso/{provider}/link`).

### Access-token claims

Identity sets `sub`, `email`, `email_verified` (true only for a verified
account), `token_type: "access"`, plus every contributor's claims (`roles`,
`permissions`, tenancy's `tenant_ids`, billing's `subscription_status` /
`plan_code`) and the provenance stamps `claims_sources` /
`claims_sources_failed`. Contributors cannot override `sub`, `email`,
`email_verified` or `token_type`.

## Published events

All identity channels use the underscore form and share one envelope schema
(`event_id`, `event_type`, `actor`, `payload`); the payload differs per event:

```
identity.user_registered.v1         # {user_id} — no email (PII stays off the bus)
identity.user_email_confirmed.v1    # {user_id}
identity.user_logged_in.v1          # {user_id, method = password|sso, provider?}
identity.user_logged_out.v1         # {user_id}
identity.user_updated.v1            # {user_id, fields}
identity.user_deleted.v1            # {user_id, mode: "soft"}
identity.user_password_reset.v1     # {user_id}
identity.user_role_assigned.v1      # {user_id, role_code}
identity.user_role_revoked.v1       # {user_id, role_code}
identity.twofa_enabled.v1           # {user_id, type: "totp"}
identity.twofa_disabled.v1          # {user_id}
identity.twofa_backup_codes_regenerated.v1
identity.sso_linked.v1              # {user_id, provider}
identity.sso_unlinked.v1            # {user_id, provider}
identity.user_sessions_revoked.v1   # {user_id, reason, tokens_valid_after}
```

`reason` is one of `deactivated`, `password_reset`, `password_changed`,
`role_revoked`.

## Audit actions

`identity.login.locked`, `identity.refresh_token.reuse_detected`,
`identity.password.reset`, `identity.password.changed`,
`identity.user.deactivated`, `identity.role.assigned`, `identity.role.revoked`,
`identity.twofa.enabled`, `identity.twofa.disabled`,
`identity.twofa.backup_codes_regenerated`, `identity.sso.linked`,
`identity.sso.unlinked`.

## Capabilities & configuration

- **Requires:** `database`, `cache`, `eventbus`. Descriptor: `required=True`.
- **Auth tokens** must carry `token_type: "access"` (guards reject otherwise).
- `TWOFA_ENCRYPTION_KEY` — key(s) encrypting TOTP secrets at rest
  (comma-separated, newest first, for rotation). **Required with
  `ENVIRONMENT=prod`**: identity registers a critical startup task that aborts
  startup without it. Outside production an unset key falls back to one derived
  from `JWT_SECRET`.
- `AUTH_REVOCATION_FAIL_CLOSED` — revocation-store outage → `503` (default: on
  in production).
- Sensitive endpoints are rate-limited per IP (e.g. login `5/60s`, fail-closed).
- JWT, SSO, and 2FA settings are in the
  [Configuration Reference](../reference/configuration.md). For OAuth2 provider
  setup, see the [SSO guide](../guides/sso-setup.md).

## How other modules use identity

Identity registers no business service contract. Other modules consume identity
only through:

- the **JWT claims** it mints (roles, permissions, `email` / `email_verified`) —
  read via the [authorization algebra](../concepts/authorization.md);
- the authenticated principal, `CurrentUser` from `sandloom.sdk` (its `.id` is the
  user id; `sandloom.sdk.web.current_user_id` is the plain dependency): it
  verifies the JWT and reads the subject's session record live, so it
  honours token revocation;
- its **events**, for reacting to user lifecycle changes
  (`identity.user_sessions_revoked.v1` to drop cached sessions).
