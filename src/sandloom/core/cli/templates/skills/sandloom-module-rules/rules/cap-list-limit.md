---
title: A list route caps its limit
impact: medium
tags: api, pagination
---

Declare a list route's `limit` with `Query(ge=1, le=<cap>)`: with no upper bound one request reads a whole table.

FastAPI refuses a larger value with 422. No check reads a route's limits.

## Incorrect

```python
@router.get("/items")
async def list_items(user: CurrentUser, limit: int = 20) -> list[ItemOut]: ...
```

## Correct

<!-- code-ok: D4 rule example, a fragment of a module file: D1 and D2 check it -->
```python
@router.get("/items")
async def list_items(
    user: CurrentUser, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[ItemOut]: ...
```
