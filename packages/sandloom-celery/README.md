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

The instance also takes `master_name` and `backend_master_name`, the Redis master of
a `sentinel://` `broker` and `backend` (required by such a url, refused with any
other scheme). A `rediss://` url must state `ssl_cert_reqs`, and
`ssl_check_hostname=true` unless it states `ssl_cert_reqs=none`: it is refused at
startup otherwise.

**macOS.** The default `prefork` pool spawns its children there, and a spawned
child never sees the task table the host registered at boot; the host refuses
to start with `prefork` on macOS. Set `worker_pool` to `solo` or `threads`.

**The importable Celery app.** `celery -A sandloom_celery.worker inspect active`
(and `call`, `status`, Flower) use the `default` app, built from the broker of the
`default` task queue (`INFRA.taskqueue.default.broker`, read through `Settings`
when the app is first used, for the `taskqueue` capability only; no `BROKER_URL`
of its own). A missing broker is an error naming that option. Celery's own CLI
writes `CELERY_BROKER_URL` from `-b` / `BROKER_URL` and `CELERY_RESULT_BACKEND`
from `--result-backend` / `RESULT_BACKEND`, and `app.conf` prefers them, so the app
refuses a `CELERY_BROKER_URL`, `CELERY_BROKER_READ_URL`, `CELERY_BROKER_WRITE_URL` or
`CELERY_RESULT_BACKEND` that differs from `INFRA`, naming the variable, never its value.
It also compares the finished `app.conf` (`broker_url`, `broker_read_url`,
`broker_write_url`, `result_backend`) with `INFRA` and refuses a difference, naming
the option: a config module (`celery --config`, `CELERY_CONFIG_MODULE`) or the default
loader (`--loader default` reads `celeryconfig.py` from the working directory) can
set them. It registers no task, and `celery -A sandloom_celery.worker worker|beat` refuses
to start: run `sandloom worker` and `sandloom beat` instead.

**No result backend.** Sandloom never reads a task result, so an instance runs
with no result backend unless it states `backend` in `INFRA`.

The tests of this driver still run with the kernel's suite; they move into this
package with its own CI lane against a real broker.
