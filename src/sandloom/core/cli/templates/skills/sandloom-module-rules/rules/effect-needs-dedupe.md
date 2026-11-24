---
title: An effect outside a durable consumer needs its own dedupe
impact: high
tags: effects, idempotency, consumers
---

An effect (a grant, a charge, a row) applied outside a durable consumer carries its own dedupe in the same unit: a UNIQUE `operation_key(...)` on its row, or `durable_inbox(...).claim(db(), id)`.

A durable consumer is claimed once per envelope id. A route, a task or a non-durable consumer is not, and a retry runs it again. A saga command step is protected only for the command it sends (runtime key `saga:<type>:<id>:<step>:do`); an effect you apply yourself in the same code still needs its own dedupe. Both helpers are in `sandloom.sdk.effects`; the inbox is named `"<module>@<channel>"`. Build the key from business ids (`operation_key` refuses a part with a colon or a space), never from a random value.

## Incorrect

```python
async def grant_credits(user_id: UUID, order_id: UUID) -> None:
    db().add(CreditGrant(user_id=user_id, amount=10))  # a retry grants twice
```

## Correct

<!-- code-ok: D4 rule example, a fragment of a module file: D1 and D2 check it -->
```python
from sandloom.sdk.effects import operation_key  # sandloom: advanced dedupe key


async def grant_credits(user_id: UUID, order_id: UUID) -> None:
    key = operation_key("grant", order_id)  # a UNIQUE column of CreditGrant
    db().add(CreditGrant(user_id=user_id, amount=10, operation_key=key))
```
