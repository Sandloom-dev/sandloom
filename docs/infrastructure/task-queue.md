---
title: Task queue
description: The task queue interface for background work and its celery driver.
type: concept
---

# Task queue

A task queue runs work outside the request. [Background tasks](../modules/background/tasks.md)
covers writing tasks; this page covers what is underneath.

## What a module calls

```python
from sandloom.sdk.infra import taskqueue

await taskqueue().enqueue(task, payload, delay=None, idempotency_key=None)  # delay: timedelta
```

## Drivers

### `celery`

Celery with a Redis broker. It ships in the `sandloom-celery` package, which
`pip install 'sandloom[celery]'` brings in. Its tier is `beta`. It supports the
features `delay`, `periodic` and `redelivery`.

| Option | Meaning |
| --- | --- |
| `broker` | the message broker URL, required; no other setting or instance supplies it |
| `backend` | the result backend URL; optional, none by default and filled from nothing: Sandloom discards task results, so most deployments leave it out |
| `dedupe_ttl_seconds` | how long an idempotency key is remembered |
| `health_timeout_seconds` | the bound of the broker probe behind `/ready`; default 5, at most 300, falls back to `HEALTH_CHECK_TIMEOUT_SECONDS` and is never above it |
| `worker_pool` | how the worker runs the messages: `prefork` (default), `solo` or `threads`; on macOS set `solo` or `threads` ([the note](../modules/background/tasks.md#the-worker-and-beat)) |

Write the broker on the instance:

```json
{"taskqueue": {"default": {"broker": "redis://broker-redis:6379/1"}}}
```

The worker (`celery -A sandloom_celery.worker`) reads the same `taskqueue.default.broker` from
`INFRA`; it takes no `BROKER_URL`.

If the broker is the same Redis host, port and database as `cache.default`, startup logs a
warning: cache eviction could drop queued messages. Give the broker its own database or server.

**Limits.** Replacing the task runtime is not a configuration change.
