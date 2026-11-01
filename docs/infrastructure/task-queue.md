---
title: Task queue
description: The task queue interface for background work and its celery driver.
type: concept
---

# Task queue

A task queue runs work outside the request. [Background tasks](../concepts/background-tasks.md)
covers writing tasks; this page covers what is underneath.

## What a module calls

```python
from sandloom.sdk.infra import taskqueue

await taskqueue().enqueue(task, payload, delay_seconds=None, idempotency_key=None)
```

## Drivers

### `celery`

Celery with a Redis broker. It supports the features `delay` and `idempotency_key`.

| Option | Meaning |
| --- | --- |
| `broker` | the message broker URL; falls back to `BROKER_URL` |
| `backend` | the result backend URL; falls back to `CACHE_URL` |
| `dedupe_ttl_seconds` | how long an idempotency key is remembered |

**Limits.** Replacing the task runtime is not a configuration change.
