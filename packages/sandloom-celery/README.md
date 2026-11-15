# Sandloom Celery driver

Install this package alongside the matching Sandloom release: `pip install
'sandloom[celery]'` brings it in, and a checkout runs `uv pip install -e
packages/sandloom -e packages/sandloom-celery` from the repository root.
Sandloom discovers the driver through the `sandloom.drivers` entry-point group.

| Entry point | Capability | Provider |
| --- | --- | --- |
| `taskqueue.celery` | `taskqueue` | `sandloom_celery.taskqueue:CeleryTaskQueueProvider` |

The driver declares the tier `beta`. Its worker and scheduler host
(`sandloom_celery.worker`, reached through `TaskQueueProvider.worker_host()`)
runs `sandloom worker` and `sandloom beat`. The `celery` instance options
`worker_pool` (`prefork`, `solo` or `threads`) and `health_timeout_seconds` are
set in `INFRA`:

```sh
INFRA='{"taskqueue":{"default":{"driver":"celery","worker_pool":"threads"}}}'
```

**macOS.** The default `prefork` pool spawns its children there, and a spawned
child never sees the task table the host registered at boot; the host refuses
to start with `prefork` on macOS. Set `worker_pool` to `solo` or `threads`.

**The importable Celery app.** `celery -A sandloom_celery.worker inspect active`
(and `call`, `status`, Flower) use the `default` app, built from `BROKER_URL`.
It registers no task, and `celery -A sandloom_celery.worker worker|beat` refuses
to start: run `sandloom worker` and `sandloom beat` instead.

The tests of this driver still run with the kernel's suite; they move into this
package with its own CI lane against a real broker.
