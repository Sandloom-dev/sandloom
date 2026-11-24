---
title: A side effect outside the database waits for the commit
impact: high
tags: effects, transactions
---

Register an email, a cache write or any call outside the database with `await on_commit(callback)`: it runs after the commit and never when the unit rolls back.

A check that must hold at the commit itself goes in `await before_commit(callback)` (`sandloom.sdk.effects`). A callback that fails is logged and does not fail the committed request, so make an effect that matters safe to retry.

## Incorrect

```python
db().add(Invite(email=email))
await cache().set(f"invite:{email}", "1")  # stays set if the unit rolls back
```

## Correct

<!-- code-ok: D4 rule example, a fragment of a module file: D1 and D2 check it -->
```python
from functools import partial

from sandloom.sdk.infra import cache  # sandloom: advanced cache write

db().add(Invite(email=email))
await on_commit(partial(cache().set, f"invite:{email}", "1"))  # after the commit only
```
