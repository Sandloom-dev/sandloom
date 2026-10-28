---
title: Tenancy
---

# Tenancy Module (`sandloom_tenancy`)

Tenancy is an **optional** module managing multi-tenant organizational
structure: organizations (tenant roots), membership with roles
(owner / admin / member), and invitations (create / accept / revoke). It exposes
a cross-module **membership read contract** so other modules (e.g. billing) can
resolve membership and roles without importing tenancy internals, and a critical
claims contributor that puts the user's organization ids (`tenant_ids`) into
access tokens.

## Domain entities

| Model | Table | Description |
| --- | --- | --- |
| `Organization` | `tenancy_organizations` | Organization / tenant root. |
| `OrganizationMember` | `tenancy_members` | User ↔ org membership with role. |
| `OrganizationInvitation` | `tenancy_invitations` | Pending / accepted / revoked invite. Only `sha256(token)` is stored (`token_hash`); a partial unique index allows one open invitation per organization + email (case-insensitive). |

## Service contract: `tenancy.membership.v1`

The sanctioned cross-module access path — consumers call
`core.get_service("tenancy.membership.v1")` rather than importing tenancy. The
registered object is the **read-only** `MembershipReadContract`; the write side
(add / change role / remove) is not reachable by other modules or RPC callers.

```python
membership = core.get_service("tenancy.membership.v1")
role = await membership.get_role(org_id, user_id)   # no session argument
```

| Method | Purpose |
| --- | --- |
| `get_role(organization_id, user_id)` | the user's role in an active org, or `None` |
| `is_member(organization_id, user_id)` | membership check |
| `has_role_at_least(organization_id, user_id, minimum)` | live role-hierarchy check |
| `list_user_organization_ids(user_id)` | active orgs a user belongs to |
| `get_member_state(organization_id, user_id)` | `MemberState(role, version)` read in one statement — what a member-event consumer stores when it refreshes on a version-less event (rule in `sandloom.shared.membership`) |

Methods take **no session**: each call runs in tenancy's own read-only Unit of
Work on tenancy's database, identically in-process and over internal RPC
(`SERVICE_BINDINGS`). A caller still passing a session gets a
`DeprecationWarning` and the session is ignored.

`tenancy.claims.v1` (auto-registered, `critical=True`) contributes `tenant_ids`;
if tenancy cannot answer, token issuance fails with `503` instead of minting a
token without the allowlist.

## HTTP API

| Router | Prefix | Endpoints |
| --- | --- | --- |
| Organizations | `/api/v1/orgs` | `POST ""`, `GET ""`, `GET /{org_id}` (member), `PATCH /{org_id}` (admin), `DELETE /{org_id}` (owner), `POST /{org_id}/transfer-ownership/{new_owner_id}` (owner) |
| Own membership | `/api/v1/orgs/{org_id}/membership` | `GET ""`: live caller role in an active organization; non-member/inactive/missing → `404` |
| Members | `/api/v1/orgs/{org_id}/members` | `GET ""` (member), `PATCH /{member_user_id}` (admin), `DELETE /{member_user_id}` (member: self = leave) |
| Invitations (org) | `/api/v1/orgs/{org_id}/invitations` | `POST ""`, `GET ""`, `DELETE /{invitation_id}` (admin) |
| Invitations (user) | `/api/v1/invitations` | `POST /accept` |

Non-members get `404` on org-scoped routes (no existence leak); a member below
the required role gets `403`.

### Rules API clients must know

- **`PATCH /api/v1/orgs/{org_id}`** accepts only `name`; any other field
  (including `is_active`) is rejected with `422`. Deactivation is the owner-only
  `DELETE`, which soft-deletes the org, publishes
  `tenancy.organization_deleted.v1` and revokes every member's access tokens (immediately, see below).
- **Roles** — only the owner may change or remove an admin. The owner role is
  never assigned through `PATCH .../members/...` (`409`); use ownership
  transfer. A role change revokes the member's access tokens (a no-op change
  revokes nobody). Roles are read live today, so this is not needed for
  correctness yet; it ends the stale session and keeps a future membership
  cache / role claims safe. **UX: the member's next request answers
  `401 "Token has been revoked"`; the client refreshes (or logs in) and
  continues under the new role.**
- **Leaving** — any member may leave with `DELETE .../members/{own user id}`;
  the owner cannot leave or be removed (`409`) and must transfer ownership
  first. Removal and leaving revoke the user's access tokens, so a stale
  `tenant_ids` claim stops working immediately (the client refreshes).
- **Immediate revocation** — removal, leaving, role change, ownership transfer
  and organization deletion revoke with `require_immediate=True`: the mark is
  written BEFORE the change commits, or the change is refused with `503` (cache
  unreachable) and rolls back. A committed change therefore always rejects the
  old tokens on the next request, in every service. A request whose guards
  passed before the change committed still completes (authorization is fresh
  when the guards run — the TOCTOU window is one in-flight request).
- **Ownership transfer** — the org row is locked and the actor re-verified as
  the current owner; the new owner must already be a member (`409` otherwise).
  The previous owner becomes admin. Two `tenancy.member_role_changed.v1` events
  are published (new owner → `owner`, previous owner → `admin`), and BOTH
  parties' tokens are revoked — the owner who made the transfer
  re-authenticates and continues as admin.
- **Invitations** — `POST .../invitations` returns the raw token once
  (`InvitationTokenOut.token`) and e-mails the accept link through the
  `notifier` capability after the request; a second open invitation for the same
  email is `409`. `POST /api/v1/invitations/accept` requires the access token's
  `email` claim to match the invited address **and** `email_verified: true`
  (`403` otherwise); an invalid / expired / used token or a deleted organization
  is `404`; already being a member is `409`. Acceptance is single-use and atomic:
  a failed attempt consumes nothing.

## Bounded lists and retries

Organization and member lists default to 100 rows, with `limit` capped at 500.
Use the returned `X-Next-Cursor` to continue. Pending invitations accept
`limit` (default 100, maximum 500) and `skip`; responses are ordered by creation
time and ID.

Send an `Idempotency-Key` on organization/member/invitation mutations to replay
successful retries. Keys are optional for compatibility; when supplied, cache
failures refuse the operation. Invitation retries with the same key replay the
original raw token rather than losing it to a duplicate-pending conflict. The
creation email is awaited by an on-commit callback, after the invitation is
durable. This replay cache does not make arbitrary effects exactly once.

`TENANT_CLAIMS_MAX_ORGANIZATIONS` defaults to 128. Claim issuance and the
membership-ID read contract refuse an oversized list rather than silently
truncating authorized tenants or minting an unbounded token. Increase the
setting deliberately, taking proxy/header limits into account.

## Published events

```
tenancy.organization_created.v1    # {organization_id, actor_id?}
tenancy.organization_updated.v1
tenancy.organization_deleted.v1
tenancy.member_added.v1            # {organization_id, user_id, role?, actor_id?, version?}
tenancy.member_removed.v1
tenancy.member_role_changed.v1
tenancy.member_added.v2            # {tenant_id, organization_id, user_id, role?, actor_id?, version}
tenancy.member_removed.v2          # dual-published with v1; version required,
tenancy.member_role_changed.v2     # so a Projection consumes them directly
tenancy.invitation_created.v1      # {organization_id, invitation_id, role?, actor_id?}
tenancy.invitation_accepted.v1
tenancy.invitation_revoked.v1
```

Invitation events carry **no email**: the `email` property stays in the v1
schema as deprecated (never emitted); resolve the invitee via `invitation_id`.

## Audit actions

`tenancy.member.role_changed`, `tenancy.member.removed`, `tenancy.member.left`,
`tenancy.organization.ownership_transferred`, `tenancy.organization.deleted`,
`tenancy.invitation.created`, `tenancy.invitation.revoked`,
`tenancy.invitation.accepted`.

## Capabilities & configuration

- **Requires:** `database`, `eventbus`. Descriptor: `required=False`.
- No tenancy-specific env vars; uses the default `database` + `eventbus`
  instances and the `notifier` for invitation e-mails (best-effort; the link is
  `{FRONTEND_URL}/invitations/accept#token=<token>` — the token is a URL
  fragment so it stays out of server logs).

## Integration with multi-tenancy

Tenancy provides the organizational model; the framework's
[tenant scoping](../concepts/multi-tenancy.md) (`TenantScoped` / `TenantScope`)
provides the fail-closed data isolation. They are complementary — tenancy is the
*who*, tenant scoping is the *enforcement*. The JWT `tenant_ids` allowlist may
lag membership changes only if the revocation store is unreachable; checks that
must observe a removal with certainty use the live contract
(`has_role_at_least` / `is_member`, or the shared `OrgRole` atom).


## Platform owner directory

The optional owner console uses dedicated platform-admin APIs (live session check), separate from
organization self-service: `GET /api/v1/platform/tenancy/organizations` (global,
including inactive organizations; bounded offset paging and `q`, `is_active`,
`owner_id` filters), `GET /organizations/{record_id}`, and `GET /members` (bounded
paging, `organization_id` / `user_id` filters). Identity must be a successful
claims source and the subject must hold the global `admin` role. Organization
ownership does not grant these APIs, and using them never creates membership.
See [operator guide](../guides/admin-operations.md).
