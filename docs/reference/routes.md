# Routes and their policy

<!-- Generated at docs build (also `sandloom routes reference`). -->

Every route of the batteries platform: who may call it (`auth`) and its resolved policy (model E, see [ADR-0009](../adr/0009-route-policy-and-session-freshness.md)). Each module approves its own routes in its `routes.snapshot.json` (`sandloom routes snapshot --write`); the kernel decides the routes it mounts.

## Kernel

| Method | Path | Auth | Policy |
|---|---|---|---|
| GET | `/api/v1/platform/billing/audit` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/consumers` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/contracts` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/modules` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/outbox/dead` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| POST | `/api/v1/platform/billing/outbox/requeue` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/platform/billing/projections` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/services` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/settings` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/streams/dlq` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| POST | `/api/v1/platform/billing/streams/replay` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/platform/identity/audit` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/identity/consumers` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/identity/contracts` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/identity/modules` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/identity/outbox/dead` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| POST | `/api/v1/platform/identity/outbox/requeue` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/platform/identity/projections` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/identity/services` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/identity/settings` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/identity/streams/dlq` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| POST | `/api/v1/platform/identity/streams/replay` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/platform/tenancy/audit` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/tenancy/consumers` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/tenancy/contracts` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/tenancy/modules` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/tenancy/outbox/dead` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| POST | `/api/v1/platform/tenancy/outbox/requeue` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/platform/tenancy/projections` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/tenancy/services` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/tenancy/settings` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/tenancy/streams/dlq` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check if a unit writes |
| POST | `/api/v1/platform/tenancy/streams/replay` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom.core.admin.api.platform.platform_operator']}, session=live, re-check before commit, write_store=default |
| POST | `/contracts/diff` | service | effect=write, idempotency=none, session=live, write_store=default |
| GET | `/contracts/persistence` | service | effect=read, idempotency=none, session=live |
| GET | `/contracts/schemas` | service | effect=read, idempotency=none, session=live |
| GET | `/diagnostics/consumers` | service | effect=read, idempotency=none, session=live |
| GET | `/diagnostics/core` | service | effect=read, idempotency=none, session=live |
| GET | `/diagnostics/infra` | service | effect=read, idempotency=none, session=live |
| GET | `/diagnostics/services` | service | effect=read, idempotency=none, session=live |
| GET | `/health` | public | effect=read, idempotency=none, public_reason=liveness / readiness probe (no caller), session=none |
| GET | `/health/simple` | public | effect=read, idempotency=none, public_reason=liveness / readiness probe (no caller), session=none |
| GET | `/metrics` | service | effect=read, idempotency=none, session=live |
| GET | `/ready` | public | effect=read, idempotency=none, public_reason=liveness / readiness probe (no caller), session=none |

## sandloom_billing

| Method | Path | Auth | Policy |
|---|---|---|---|
| POST | `/api/v1/billing/checkout-session` | authenticated | effect=write, idempotency=explicit in-flight, fail-closed, operation_intent=PSP session issued under a persisted intent, session=live, release_check, write_store=default |
| GET | `/api/v1/billing/credits` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| POST | `/api/v1/billing/customer-portal` | authenticated | effect=write, idempotency=explicit in-flight, fail-closed, operation_intent=PSP session issued under a persisted intent, session=live, release_check, write_store=default |
| GET | `/api/v1/billing/plans` | public | effect=read, idempotency=none, public_reason=public plan catalog (pricing page), session=none |
| GET | `/api/v1/billing/subscription` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| POST | `/api/v1/billing/webhooks/stripe` | public | effect=write, idempotency=none, public_reason=PSP webhook; the Stripe signature is verified in the endpoint, session=none, write_store=default |
| GET | `/api/v1/platform/billing/balances` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/balances/{record_id}` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/customers` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/customers/{record_id}` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| POST | `/api/v1/platform/billing/customers/{record_id}/adjust-credits` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check every unit, write_store=default |
| GET | `/api/v1/platform/billing/grants` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/grants/{record_id}` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/intents` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/intents/{record_id}` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/ledger` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/ledger/{record_id}` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/payment-states` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/prices` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/prices/{record_id}` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/products` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/products/{record_id}` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| PATCH | `/api/v1/platform/billing/products/{record_id}` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check every unit, write_store=default |
| GET | `/api/v1/platform/billing/provider` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/subscriptions` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/subscriptions/{record_id}` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/webhooks` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/billing/webhooks/{record_id}` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |

## sandloom_identity

| Method | Path | Auth | Policy |
|---|---|---|---|
| GET | `/api/v1/permissions` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check if a unit writes |
| POST | `/api/v1/permissions` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/roles` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check if a unit writes |
| POST | `/api/v1/roles` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check before commit, write_store=default |
| DELETE | `/api/v1/roles/{role_id}` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/roles/{role_id}` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check if a unit writes |
| PUT | `/api/v1/roles/{role_id}` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check before commit, write_store=default |
| DELETE | `/api/v1/roles/{role_id}/permissions/{permission_id}` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check before commit, write_store=default |
| POST | `/api/v1/roles/{role_id}/permissions/{permission_id}` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/users` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check if a unit writes |
| DELETE | `/api/v1/users/{user_id}` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/users/{user_id}` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| PUT | `/api/v1/users/{user_id}` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sensitive()']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/users/{user_id}/block` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| POST | `/api/v1/users/{user_id}/block` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check before commit, write_store=default |
| POST | `/api/v1/users/{user_id}/reset-2fa` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check before commit, write_store=default |
| POST | `/api/v1/users/{user_id}/revoke-sessions` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/users/{user_id}/roles` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| DELETE | `/api/v1/users/{user_id}/roles/{role_id}` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check before commit, write_store=default |
| POST | `/api/v1/users/{user_id}/roles/{role_id}` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sandloom.shared.authz.deps.require.<locals>._claims_guard']}, session=live, re-check before commit, write_store=default |
| POST | `/api/v1/users/{user_id}/unblock` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check before commit, write_store=default |
| GET | `/auth/2fa/backup-codes` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| POST | `/auth/2fa/backup-codes/regenerate` | authenticated | effect=write, idempotency=automatic S, session=live, re-check before commit, write_store=default |
| POST | `/auth/2fa/disable` | authenticated | effect=write, idempotency=automatic S, session=live, re-check before commit, write_store=default |
| POST | `/auth/2fa/enable` | authenticated | effect=write, idempotency=automatic S, session=live, re-check before commit, write_store=default |
| POST | `/auth/2fa/init` | authenticated | effect=write, idempotency=automatic S, session=live, re-check before commit, write_store=default |
| GET | `/auth/2fa/status` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| POST | `/auth/2fa/verify` | authenticated | effect=write, idempotency=automatic S, session=live, re-check before commit, write_store=default |
| POST | `/auth/change-password` | authenticated | effect=write, idempotency=automatic S, session=live, re-check before commit, write_store=default |
| GET | `/auth/health` | public | effect=read, idempotency=none, public_reason=module liveness probe (no caller), session=none |
| POST | `/auth/login` | public | effect=write, idempotency=none, issues_tokens=True, public_reason=password login: the caller has no token yet, session=none, write_store=default |
| POST | `/auth/logout` | public | effect=write, idempotency=none, public_reason=logout: the refresh token in the body is the credential, session=none, write_store=default |
| GET | `/auth/me` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| PATCH | `/auth/me` | authenticated | effect=write, idempotency=automatic S, session=live, re-check before commit, write_store=default |
| POST | `/auth/refresh` | public | effect=write, idempotency=none, issues_tokens=True, public_reason=refresh-token exchange: the refresh token is the credential, session=none, write_store=default |
| POST | `/auth/register` | public | effect=write, idempotency=none, public_reason=sign-up: the caller has no account yet, session=none, write_store=default |
| POST | `/auth/request-password-reset` | public | effect=write, idempotency=none, public_reason=password reset: the caller cannot log in, session=none, write_store=default |
| POST | `/auth/resend-verification` | public | effect=write, idempotency=none, public_reason=unverified accounts cannot log in yet, session=none, write_store=default |
| POST | `/auth/reset-password` | public | effect=write, idempotency=none, public_reason=password reset: the reset token is the credential, session=none, write_store=default |
| GET | `/auth/sso/me/linked` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| POST | `/auth/sso/mfa` | public | effect=write, idempotency=none, issues_tokens=True, public_reason=SSO MFA step: the MFA challenge token is the credential, session=none, write_store=default |
| GET | `/auth/sso/providers` | public | effect=read, idempotency=none, public_reason=login page: list the SSO providers, session=none |
| GET | `/auth/sso/{provider}/callback` | public | effect=read, idempotency=none, issues_tokens=True, public_reason=SSO callback: provider assertion + binding cookie, session=none |
| POST | `/auth/sso/{provider}/link` | authenticated | effect=write, idempotency=automatic S, session=live, re-check before commit, write_store=default |
| GET | `/auth/sso/{provider}/link/redirect` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| GET | `/auth/sso/{provider}/redirect` | public | effect=read, idempotency=none, public_reason=SSO login starts before the caller has a token, session=none |
| DELETE | `/auth/sso/{provider}/unlink` | authenticated | effect=write, idempotency=automatic S, session=live, re-check before commit, write_store=default |
| POST | `/auth/verify-email` | public | effect=write, idempotency=none, public_reason=e-mail verification: the signed link token is the credential, session=none, write_store=default |

## sandloom_tenancy

| Method | Path | Auth | Policy |
|---|---|---|---|
| POST | `/api/v1/invitations/accept` | authenticated | audit=automatic, effect=write, idempotency=automatic S, risk={'admin': ['sensitive()']}, session=live, re-check every unit, write_store=default |
| GET | `/api/v1/orgs` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| POST | `/api/v1/orgs` | authenticated | effect=write, idempotency=explicit cache, fail-closed, session=live, re-check before commit, write_store=default |
| DELETE | `/api/v1/orgs/{org_id}` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom_tenancy.access.require_org_role.<locals>._guard']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/orgs/{org_id}` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| PATCH | `/api/v1/orgs/{org_id}` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom_tenancy.access.require_org_role.<locals>._guard']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/orgs/{org_id}/invitations` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom_tenancy.access.require_org_role.<locals>._guard']}, session=live, re-check if a unit writes |
| POST | `/api/v1/orgs/{org_id}/invitations` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom_tenancy.access.require_org_role.<locals>._guard']}, session=live, re-check before commit, write_store=default |
| DELETE | `/api/v1/orgs/{org_id}/invitations/{invitation_id}` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom_tenancy.access.require_org_role.<locals>._guard']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/orgs/{org_id}/members` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| DELETE | `/api/v1/orgs/{org_id}/members/{member_user_id}` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sensitive()']}, session=live, re-check before commit, write_store=default |
| PATCH | `/api/v1/orgs/{org_id}/members/{member_user_id}` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom_tenancy.access.require_org_role.<locals>._guard']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/orgs/{org_id}/membership` | authenticated | effect=read, idempotency=none, session=live, re-check if a unit writes |
| POST | `/api/v1/orgs/{org_id}/transfer-ownership/{new_owner_id}` | authenticated | audit=automatic, effect=write, idempotency=explicit cache, fail-closed, risk={'admin': ['sandloom_tenancy.access.require_org_role.<locals>._guard']}, session=live, re-check before commit, write_store=default |
| GET | `/api/v1/platform/tenancy/members` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/tenancy/organizations` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
| GET | `/api/v1/platform/tenancy/organizations/{record_id}` | authenticated | effect=read, idempotency=none, risk={'admin': ['sandloom.shared.guards.platform.require_platform_admin']}, session=live, re-check if a unit writes |
