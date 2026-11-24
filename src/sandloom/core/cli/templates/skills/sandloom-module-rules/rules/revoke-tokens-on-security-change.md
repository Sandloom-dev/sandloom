---
title: A password, role or membership change revokes the user's tokens
impact: high
tags: security, auth, sessions
---

After a password, role or membership change, call `revoke_user_tokens_on_commit(..., require_immediate=True)` as the last step that can fail, and `record_audit` the change.

Tokens already issued keep the old rights until they expire. The function is in `sandloom.sdk.security`; the revocation is skipped when the unit rolls back, and `require_immediate=True` refuses the change (503) when the revocation cannot be written. The audit record carries no personal data.

## Incorrect

```python
async def change_role(member: Member, role: str) -> None:
    member.role = role  # the member's tokens still carry the old role
```

## Correct

<!-- code-ok: D4 rule example, a fragment of a module file: D1 and D2 check it -->

```python
from sandloom.sdk import security  # sandloom: advanced revocation


async def change_role(member: Member, role: str) -> None:
    member.role = role
    record_audit("member.role_changed", resource_id=str(member.id))  # no PII
    await security.revoke_user_tokens_on_commit(
        member.user_id, reason="role changed", require_immediate=True
    )  # the last step that can fail
```
