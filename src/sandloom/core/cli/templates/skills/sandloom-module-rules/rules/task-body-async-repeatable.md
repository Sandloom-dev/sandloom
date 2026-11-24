---
title: A task body is async, opens its own unit and can run twice
impact: medium
tags: tasks, units, idempotency
---

A task body is an `async def` that works in `async with open_uow():`, never `asyncio.run`, and a repeated run does no harm.

The worker already has an event loop, and `db()` raises outside a unit. A task can run more than once for one enqueue (with `at_least_once=True`, or a periodic slot sent twice). Make a repeat harmless by deleting or updating by a condition, an upsert, or a dedupe key (rule `effect-needs-dedupe`). Send a task with `await task.enqueue(payload)`. A periodic task needs one beat per deployment profile (SANDLOOM-T010; `sandloom add task --periodic` wires it).

## Incorrect

<!-- code-ok: D4 rule example, a fragment of a module file: D1 and D2 check it -->
```python
@task()
def send_digest(payload: DigestIn) -> None:
    asyncio.run(build_digest(payload))  # a second event loop, and no unit
```

## Correct

<!-- code-ok: D4 rule example, a fragment of a module file: D1 and D2 check it -->
```python
@task(at_least_once=True)
async def send_digest(payload: DigestIn) -> None:
    async with open_uow():  # one short unit
        await build_digest(payload)  # harmless when it runs twice
```
