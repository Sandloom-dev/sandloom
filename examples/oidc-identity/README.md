# oidc-identity

A Sandloom application that has **no `sandloom_identity` module**. Users sign in
at an external OpenID Connect provider (Keycloak, Auth0, Entra ID or similar).
The application's own module `acme_idp` takes the place of identity: it checks
the provider's `id_token`, maps the subject to a local account and issues this
application's access token. The standard `sandloom_tenancy` and `sandloom_billing`
modules run unchanged next to it.

The point of the example: replacing a bundled module needs **no change to the
kernel (`sandloom.core`, `sandloom.sdk`, `sandloom.shared`) and no change to the
batteries**. The proof is `tests/test_oidc_replacement.py`, which runs the
application as real processes against PostgreSQL and Redis.

## What is in it

| Path | What it is |
| --- | --- |
| `oidc_identity/asgi.py` | The composition root and the claim-authority map |
| `oidc_identity/modules/acme_idp/` | The issuer module (third-party code, not a battery) |
| `oidc_identity/modules/storefront/` | A consumer module whose routes use the kernel's `Perm` and the generated `Subscribed` and `OrgRole` atoms |
| `tests/_oidc_issuer.py` | A local OIDC provider: discovery, JWKS, RS256 keys only it holds. It knows nothing about Sandloom |
| `tests/test_oidc_replacement.py` | The proof, in two topologies (mono and split) |

`acme_idp` has one table (`acme_idp_accounts`, the local account of an external
`issuer` + `subject`) and three routes:

- `POST /api/v1/acme_idp/login`: exchanges an `id_token` for an access token.
  The `id_token` is verified through the provider's discovery document and JWKS
  (`iss`, `aud`, `exp`, `kid`, signature). Members of the provider group named by
  `ACME_IDP_ADMIN_GROUP` get the platform role `admin`. There is no refresh token:
  the client signs in again.
- `POST /api/v1/acme_idp/backchannel-logout`: the provider's OIDC back-channel
  logout. It revokes every access token of the subject.
- `POST /api/v1/acme_idp/accounts/{user_id}/block`: an emergency block by a platform
  administrator.

Settings: `ACME_IDP_ISSUER` (the provider's issuer URL), `ACME_IDP_CLIENT_ID` (the
`aud` an `id_token` must carry), `ACME_IDP_ADMIN_GROUP` (default `platform-admins`),
`ACME_IDP_JWKS_TTL_SECONDS` (default 300).

## How the map names the issuer

Every protected token claim has one authority, written in code in
`oidc_identity/asgi.py` (ADR-0014):

```python
IDP = Authority("acme_idp", module="oidc_identity.modules.acme_idp")

CLAIM_AUTHORITIES = {
    "issuer": IDP,  # acme_idp mints the token
    "roles": IDP,
    "permissions": IDP,
    "tenant_ids": "sandloom_tenancy",
    "subscription_status": "sandloom_billing",
    "plan_code": "sandloom_billing",
    "features": "sandloom_billing",
    "limits": "sandloom_billing",
}
```

- `issuer` names the module that issues tokens. `email` and `email_verified` are
  not listed: a map that names an issuer gives those two keys to it.
- `Authority("acme_idp", module=...)` carries the public name. A process that
  does not load acme_idp (P2 in split, or an operator command there) still
  knows its operator contract is `acme_idp.security.v1`; an import path alone
  names nothing outside the process that loads it.
- A contributor that writes a claim it is not assigned loses its whole result.
  Startup refuses a map the composition cannot honour (`sandloom explain
  SANDLOOM-B035`): the process that loads the issuer needs every named authority
  loaded or reachable over a service binding.
- `acme_idp` registers its claims contributor as critical. A sign-in whose rights
  cannot be read is refused, not issued without them.
- `acme_idp` declares `refresh_sessions=False`, so an emergency block needs no
  refresh-session consumer.

Removing a tenant membership revokes all existing access tokens for that user.
Because acme_idp keeps no refresh session, the client signs in at the external
provider again. It exchanges the provider-issued `id_token` for a token with
the remaining memberships.

If your issuer keeps the provider's refresh token to renew without another
sign-in, declare `refresh_sessions=True`. Register a durable consumer of
`core.auth.user_blocked.v1` that ends those refresh sessions when the user is
blocked. Otherwise, keep the stateless declaration and accept another sign-in.

## What the test proves

In both topologies:

- **Sign-in.** A valid `id_token` gives a token whose `roles`, `permissions`,
  `email` come from `acme_idp`, `tenant_ids` from tenancy and the subscription
  claims from billing. A forged, expired, foreign or mis-addressed `id_token` is
  refused. A key the provider rotated is fetched.
- **Tenancy.** An organization created with the token, the `tenant_ids` grant and
  `TenantScope`. An invitation is accepted on the `email` / `email_verified` that
  `acme_idp` issued; a wrong or unverified address is refused.
- **Billing.** `Subscribed` (read from the claim) answers 402, then 200 once a
  subscription is active. `OrgRole` and `SubscriptionActive` run live over
  contracts, and billing's own organization-scoped read works.
- **Revocation, three ways.** Removing a member revokes their tokens (tenancy).
  The provider's back-channel logout revokes a subject's tokens (`acme_idp`). An
  emergency block refuses the account's tokens and its next sign-in. A new sign-in
  after a logout works again.
- **Operator commands.** `sandloom security revoke-sessions --user ID` finds
  `acme_idp` as the issuer and calls its `acme_idp.security.v1`
  (`end_sessions`; `list_user_ids` for `--all`). The kernel revokes the access
  tokens in every process, and the report reads: access tokens revoked /
  refresh sessions not applicable / external IdP sessions not controlled / new
  sign-in still possible. `force-password-reset` is refused before anything is
  done (`SANDLOOM-B039`: acme_idp keeps no passwords), a dry run too. In split
  the command runs in P2, which does not load acme_idp, and reaches it over RPC.

The two topologies:

- **mono**: one process loads every module.
- **split**: process 1 loads `acme_idp` and `storefront`, process 2 loads tenancy
  and billing. They talk over the signed internal RPC, and the access tokens are
  EdDSA tokens whose private key only process 1 holds, so process 2 verifies what
  `acme_idp` issued with the public key alone.

## Run it

From the repository root, start the test services and install:

```bash
uv sync --locked
docker compose -f deploy/compose/test.yml up -d --wait
```

The proof and the application's full gate (generated files, ruff, mypy,
`sandloom check`, snapshots, migration drift, the whole suite):

```bash
cd examples/oidc-identity
uv run sandloom test --full
```

Only the proof (mono and split):

```bash
uv run pytest tests/test_oidc_replacement.py -p no:cacheprovider
```

The test starts its own provider and its own database, so it needs only
PostgreSQL and Redis (`SANDLOOM_TEST_DATABASE_URL`, `SANDLOOM_TEST_CACHE_URL`,
`SANDLOOM_TEST_BROKER_URL` on a shared machine). It skips without them unless
`SANDLOOM_E2E_REQUIRED=1`.

### Mono, against a real provider

```bash
cd examples/oidc-identity
uv sync
cp .env.example .env
# Set DATABASE_URL, CACHE_URL, BROKER_URL, JWT_SECRET (openssl rand -hex 32),
# ACME_IDP_ISSUER and ACME_IDP_CLIENT_ID for your provider.
uv run sandloom migrate upgrade-all --apply
uv run uvicorn oidc_identity.asgi:app --reload
```

### Split

The same `oidc_identity.asgi:app` serves every process. `SANDLOOM_MODULES` picks
what a process loads:

- process 1: `["oidc_identity.modules.acme_idp", "oidc_identity.modules.storefront"]`
- process 2: `["sandloom_tenancy", "sandloom_billing"]`

Process 1 binds every contract the batteries provide to process 2 with
`SERVICE_BINDINGS`, and both use per-service RPC keys. The `split()` function in
`tests/test_oidc_replacement.py` builds the complete environment of both
processes (bindings, allowlist, RPC keys, the EdDSA JWT key); the guides
[split into services](../../docs/operate/split-into-services.md) and
[service RPC](../../docs/operate/service-rpc.md) describe each setting.

## Current limits

- `sandloom security grant-admin` works here: acme_idp finds the account and
  holds the `admin` role. The role set is the provider's groups, read again at
  every sign-in, so a grant lasts until the account's next sign-in unless the
  provider's group carries it too. `sandloom admin list` shows the
  administrators (password: "at the provider"). `sandloom admin create` and
  `invite` are refused before anything is done: acme_idp creates no account and
  keeps no password (people sign in at the provider). A revocation is not a
  block: use the block route above to refuse the next sign-in.
- The **admin console** still signs in through identity's `/auth` paths. Login
  through the issuer named in the map is task `kb-proto-console-login`.
- `acme_idp` is an example, not a hardened product: no refresh tokens, no account
  linking across providers, and the admin role comes from one provider group.
