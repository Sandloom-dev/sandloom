---
title: Revoke sessions
description: Revoke user sessions safely from application code or an operator command, with transaction and production safeguards.
---

# Revoke sessions

A signed JWT can remain cryptographically valid after a password change or a
membership change. Sandloom also checks the subject's live security state.
Revocation invalidates existing sessions; the user must authenticate again.

## Application changes

Security-sensitive changes use `revoke_user_tokens_on_commit` from
[`sandloom.sdk.security`](../reference/sdk/security.md). Call it inside the
owning unit as part of the security action, with an explicit reason. Password,
role and membership revocation paths require immediate fail-closed protection;
keep `require_immediate=True` for those paths. The durable event provides the
cross-service follow-up. Record the action in the audit log.

Use `revoke_user_tokens` for an intentional immediate revocation outside that
transactional change path. See the exact signatures and semantics in the SDK
reference; neither key rotation nor deleting an arbitrary cache entry is a
replacement for session revocation.

## Operator action

The operator CLI defaults to a dry run. Select exactly the intended scope:
`--user <UUID>`, `--tenant <UUID>` or `--all`. Inspect the plan before applying:

```bash
uv run sandloom security revoke-sessions --user <UUID> --json
uv run sandloom security revoke-sessions --user <UUID> --apply --reason "INC-123 credential compromise"
```

Replace `<UUID>` with the affected user ID. In production, `--apply` additionally
requires `--confirm-production`. `--operator` records who acts;
`--operation-id` resumes an interrupted operation and `--batch` bounds its work.
The [operator reference](../operations/operator-reference.md) documents the
remaining incident commands and confirmation rules.

## Verify the outcome

A previously issued token must fail, and a newly authenticated session must
follow the current roles and memberships. Check the security audit and the
revocation consumer's health. An unavailable security store fails closed;
readiness can degrade while a durable revocation is pending. Process liveness
remains `/health/simple`.

For recovery after a Redis restore, follow
[the restore procedure](../operations/operator-reference.md#restoring-the-order-and-the-traffic-gate)
before accepting traffic.
