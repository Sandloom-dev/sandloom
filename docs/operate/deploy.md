---
title: Deploy an application
description: Run a Sandloom application in production with its generated monolith profile - required variables, preflight, background work, the image and the hardening checklist.
type: how-to
---

# Deploy an application

`sandloom new` generates a production profile, `deploy/docker-compose.yml`, and a
`Dockerfile`. The profile runs every module in one process with one PostgreSQL
database and one Redis. To run modules as separate services, deploy this way
first and then [split the application](split-into-services.md).

## Start it

From the project root:

```bash
export JWT_SECRET=$(openssl rand -hex 32) INTERNAL_API_TOKEN=$(openssl rand -hex 32)
docker compose -f deploy/docker-compose.yml up --build
```

The `migrate` one-shot runs `sandloom migrate upgrade-all --apply` first. The
`app` (port 8000), `worker` and `beat` services start after it completes.

The profile sets `ENVIRONMENT=prod`, so the production checks apply: a missing,
short or placeholder secret stops the process at startup.

| Variable | Needed | Notes |
| --- | --- | --- |
| `JWT_SECRET` | always | signs access tokens |
| `INTERNAL_API_TOKEN` | always | unlocks `/diagnostics`, `/contracts`, `/metrics` and `/health` details; it never authenticates calls between services |
| `TWOFA_ENCRYPTION_KEY` | with identity | encrypts TOTP secrets at rest |
| `BILLING_PROVIDER` | with billing | `stripe`, with `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` |

For a demo without Stripe, set `BILLING_PROVIDER=fake` and
`SANDLOOM_ACCEPTED_RISKS=local_storage_in_prod,fake_psp_in_prod`. Each accepted
risk is logged at every start.

Keep real secrets in files rather than in the environment: see
[Secrets management](operator-reference.md#secrets-management).

## Check it

```bash
docker compose -f deploy/docker-compose.yml exec worker sandloom doctor
```

`sandloom doctor` is a read-only preflight: configuration, modules, reachable
backends, migration heads, consumer lag, row-level security and sagas. It exits
`0` when everything is fine, `1` on warnings and `2` on failures. A fresh profile
exits `1` with one warning: the accepted `local_storage_in_prod` risk, explained below.
See
[Preflight](operator-reference.md#preflight-and-effective-configuration).

## Background work

The `worker` and `beat` services run the framework's Celery entrypoint for any
application:

```bash
celery -A sandloom.core.worker worker --loglevel=INFO
celery -A sandloom.core.worker beat   --loglevel=INFO
```

`sandloom.core.worker` boots your application (`SANDLOOM_COMPOSITION_ROOT`, set to
`<app>.asgi` in the profile) with the process's modules and imports the `tasks.py`
of each loaded module. Run **exactly one beat** per broker. See
[Background tasks](../modules/background/tasks.md).

## The image

The generated `Dockerfile` builds from `uv.lock` when it exists, runs as a non-root
user and serves with gunicorn and uvicorn workers. The worker count follows the
container's CPU quota; a non-empty `WEB_CONCURRENCY` overrides it, and the profile
sets it to 2. Keep
`workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW)` under PostgreSQL's `max_connections`.

## Before real traffic

- Terminate TLS at the gateway or an upstream load balancer.
- Behind a proxy, set `TRUST_PROXY_HEADERS=true` **and** `TRUSTED_PROXY_IPS` (the
  proxies' CIDRs). See
  [Security model](../reference/security-model.md#transport-request-hardening).
- Probe `/health/simple` for liveness and `/ready` for readiness. `/health` and
  `/ready` answer `503` when degraded, so using either as a liveness probe restarts
  the process during a database outage.
- Scrape `/metrics` with `METRICS_TOKEN`, which must differ from
  `INTERNAL_API_TOKEN`. Public callers of `/health` and `/ready` see statuses only;
  details require `X-Internal-Token`.
- Use managed or replicated PostgreSQL with a tested backup: the profile's
  `postgres_data` volume is not a backup policy. See
  [Backups](operator-reference.md#backups-restore).
- Choose how email is sent. The default `log` driver only writes messages to the log,
  so sign-up and password-reset emails from identity don't reach anyone until you set
  `NOTIFIER_BACKEND` and `SMTP_*`. See
  [Notifications](../infrastructure/notifications.md).
- Choose where files live. The default object store is a local volume on one host, which
  is what the `local_storage_in_prod` risk accepts. See
  [Object store](../infrastructure/object-store.md).
- Run a shared multi-tenant product with
  `PRODUCTION_PROFILE=multi_tenant_strict`: row-level security on every tenant table,
  fail-closed revocation and a separate non-evicting security Redis. See
  [Production profile](../reference/security-model.md#production-profile-the-multi-tenant-trust-model-as-a-contract).

## The framework's own profiles

The framework repository has its own `deploy/compose/monolith.yml` and
`microservices.yml`, with the standard modules. Every variable and sizing rule is in
[`deploy/README.md`](https://github.com/Sandloom-dev/sandloom/blob/main/deploy/README.md).
