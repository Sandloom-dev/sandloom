---
title: Deployment topologies
description: One codebase as a monolith or as services - SANDLOOM_MODULES, SERVICE_BINDINGS, the deploy profiles and the invariants they keep.
type: concept
---

# Deployment topologies

Sandloom is a **configurable monolith**: one codebase and one image deploy as a
single process or as a set of services, by configuration alone.
`SANDLOOM_MODULES` (a JSON list) decides which modules a process loads;
`SERVICE_BINDINGS` tells a process where to reach the contracts it calls but
does not host. Modules talk only through events and contracts, which behave
the same locally and remotely, so moving a module into its own service needs
no code change. Moving that module's existing data into its own database is
a separate, offline step (`sandloom migrate extract-module`).

## What you get by default / what you declare

| You get by default | You declare |
| --- | --- |
| Every module of the app loaded in one process | `SANDLOOM_MODULES` to load a subset per process |
| Contracts resolved locally first | `SERVICE_BINDINGS` for each consumed contract a process does not host |
| Startup refusal when a required consumed contract is neither local nor bound | `Contract(REF, optional=True)` only where "not deployed" has a correct answer |
| Events over the durable Redis Streams bus in production, each database with its own outbox and relay | nothing |
| One migrate one-shot per database: the core chain plus the chains of the modules it hosts | the module's database (see [Migrations](../modules/database/migrations.md)) |
| Production validators on every process environment | the required secrets and keys of the profile |

## How to use it

### Topologies

| Profile | File | Topology |
| --- | --- | --- |
| Monolith | `deploy/docker-compose.yml` in an application, `deploy/compose/monolith.yml` in the framework | `migrate`, then `app` (every module) plus `worker` and `beat`; one PostgreSQL database; Redis |
| Microservices | `deploy/compose/microservices.yml`, written by `sandloom add deploy` | an nginx gateway in front of one module per service, each with its own database and its own `migrate-<service>` one-shot, wired by `SERVICE_BINDINGS`, on the Streams bus |

Both profiles run with `ENVIRONMENT=prod`, require `TWOFA_ENCRYPTION_KEY` and
an explicit `BILLING_PROVIDER`, and start the app and worker only after their
migrate one-shot completed (`sandloom migrate upgrade-all --apply`,
`depends_on: service_completed_successfully`).

- **Monolith** shares `JWT_SECRET` (HS256) and needs no RPC keys (no
  bindings).
- **Microservices** sign tokens with EdDSA: `JWT_PRIVATE_KEY` and
  `JWT_SECRET` on the identity processes only, `JWT_PUBLIC_KEYS` and
  `JWT_KID` everywhere. They also require `INTERNAL_API_TOKEN` (ops
  endpoints) and per-service RPC keys: `RPC_DH_PRIVATE_KEY` on each service,
  `RPC_PRIVATE_KEY` on each calling service (it signs commands), and the
  shared `RPC_SERVICE_DH_KEYS`, `RPC_SERVICE_KEYS` and `RPC_ALLOWED_CALLS`,
  which must cover the services' `SERVICE_BINDINGS`. `sandloom keys generate
  --purpose rpc` and `sandloom keys allowlist` produce them.

The authoritative description of both profiles (variables, sizing, gateway
and IP trust, the extraction checklist) is `deploy/README.md` in the
repository. The steps are in [Deploy an application](deploy.md) and
[Split into services](split-into-services.md).

### How a split works

1. **Loading.** Each process loads the modules in `SANDLOOM_MODULES` and nothing
   else: only their routes, events, consumers and contracts exist there.
2. **Events** ride the durable Streams bus. Each service's outbox lives in
   its own database and its relay dispatches it. Consumer groups are
   namespaced by module, so a module's consumers never share a group with
   another module's (see [Events](../modules/between/events.md)).
3. **Contracts** resolve local-first: a co-deployed provider wins; otherwise
   `SERVICE_BINDINGS` supplies a generated RPC proxy, authenticated per
   service pair and pinned by `"service_id"`; otherwise the contract is not
   deployed. A required contract then refuses startup; an optional one is
   `None`. See [Contracts](../modules/between/contracts.md) and
   [Service RPC](service-rpc.md).
4. **Routing.** The gateway maps path prefixes to services; adding a service
   adds a location block. It never routes `/_sandloom`, `/diagnostics` or
   `/metrics`, and re-resolves services through Docker DNS.

`SERVICE_BINDINGS` is validated strictly at startup: per binding only
`base_url` / `url` (absolute http(s)), `timeout_seconds`, `critical`,
`required` and `service_id` are accepted.

### Invariants the profiles keep

- **Shared security settings.** Every security- or auth-relevant setting
  (`JWT_*` except the issuer-only keys, token lifetimes,
  `INTERNAL_API_TOKEN`, notifier and SMTP, `BILLING_DENY_ON_PAST_DUE`) lives
  in the shared environment anchor and is identical in every process.
- **One cache.** `CACHE_URL` is shared by all services: revocation state,
  rate limits, idempotency records and the bus live there.
- **Probes.** Peers never feed `/health` (operational health); a down peer fails
  `/ready` only for a binding marked `"required": true`, probed concurrently
  with a cap. `/health` and `/ready` answer 503 when degraded (the
  Kubernetes probe contract).
- **Workers** are sized from the CPU quota (`WORKER_CONCURRENCY` overrides)
  and health-checked with `python -m sandloom.core.worker healthcheck`; exactly
  one beat per broker holds the lease (see
  [Background tasks](../modules/background/tasks.md)).
- **The image** builds from the locked `uv.lock`, runs as a non-root user and
  serves with gunicorn and uvicorn workers (cgroup-aware default worker
  count; `WEB_CONCURRENCY` overrides). CI builds the production image and
  runs a blocking dependency vulnerability audit.

### How the framework proves it

- Subset loading: any module subset boots with exactly its own routes.
- Each standard module boots **alone** through `create_app` and its lifespan,
  refusing only with the documented binding error for its required consumed
  contracts; any error logged at startup fails the test. Example apps run
  the same gate.
- The **split conformance matrix** boots every subset of the standard
  identity, tenancy and billing modules as real processes, bound to a peer
  and alone. A bound subset must answer a request script identically to the
  monolith; an unbound one identically or with an explicit 404 / 501 / 503,
  never a 500 and never a different 2xx. A new cross-module route or gate
  belongs in that script.
- Every process environment of both profiles validates against the
  production settings.

### Moving a module into its own service

`sandloom add deploy` writes the service, its database and migrate one-shot, the
bindings, the allowlist and the gateway routes: see
[Split into services](split-into-services.md). A module that already
holds data needs one more step, [moving its data](split-into-services.md#existing-data).

## What breaks it

| Code or error | When |
| --- | --- |
| [SANDLOOM-B005](../reference/error-codes.md#sandloom-b005) | a required consumed contract is neither provided locally nor bound |
| [SANDLOOM-R005](../reference/error-codes.md#sandloom-r005) | a call to a contract that is not deployed |
| [SANDLOOM-T003](../reference/error-codes.md#sandloom-t003) | a module that no deploy profile deploys (warning) |
| `RemoteServiceUnavailable` (503) | a bound provider is down, times out or rejects the caller's authentication |
| startup refusal | an invalid `SERVICE_BINDINGS` entry, a caller without its RPC key, an allowlist that does not cover the bindings, a consumer group already owned by another module |

??? note "Guarantees, precisely"
    - **Same code, any subset (C-SPLIT-01, 03).** Bound subsets answer the
      request script identically to the monolith; every subset, and each
      module alone, boots and becomes ready, refusing only with the
      documented binding error.
    - **Resolution and probes (C-SPLIT-02, 04, 05).** Local-first
      resolution; peers never feed `/health`; strict binding validation.
    - **Shipped profiles (C-SPLIT-06, 07, C-DEPLOY-01 to 05).** Each service
      binds exactly its consumed-but-not-local contracts; a token issuer
      deployed alone collects claims over RPC; the monolith and microservices
      stacks boot and fit the connection budget.
    - **Contracts across a split (G-CONTRACT-12, G-COMPAT-04 to 06).**
      Required contracts are present at startup; incompatible remote
      providers fail `/ready` (or startup with `strict`).
    - **Not provided:** atomicity across services. Each database has its own
      unit; cross-service consistency is a [saga](../modules/between/sagas.md) or event
      choreography.

    See the [guarantee ledger](../reference/guarantees.md).

## Design rationale

- [ADR-0001: framework distribution](../adr/0001-framework-distribution.md).
- [ADR-0004: RPC pair-MAC request authentication](../adr/0004-rpc-pair-mac-request-authentication.md).
