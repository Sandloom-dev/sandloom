---
title: SLOs
---

# Service level objectives

Sandloom exports the metrics an SLO needs; it does not choose your objectives.
This page defines **SLIs** on the shipped metrics, gives **example SLOs**
with starting targets measured on a small stand, and a template for the
**error budget** and its policy. The SLIs are Prometheus recording rules in
`deploy/observability/prometheus/rules/sandloom-slo.yml`; the *Sandloom / SLOs*
Grafana dashboard plots their budgets and burn rates.

!!! warning "Examples, not promises"
    Every target below is an **example**. It comes from one qualification run
    of a 2 vCPU monolith (see [Where the numbers come from](#where-the-numbers-come-from)),
    not from your hardware, traffic mix or users' expectations. Start with
    them, measure for a few weeks, then set your own.

## SLIs

An SLI is the share of **good** events among valid events. The recording rules
store the **bad** share (`1 - good/valid`) per `job`, over 5m, 30m, 1h and 6h
windows, as `sandloom:sli_<name>:ratio_rate<window>`.

| SLI | Valid events | Bad events | Recording rule |
|---|---|---|---|
| API read availability | `sandloom_http_requests_total`, `method` GET/HEAD | `status` 5xx | `sandloom:sli_read_errors:ratio_rate*` |
| API write availability | `method` POST/PUT/PATCH/DELETE | `status` 5xx | `sandloom:sli_write_errors:ratio_rate*` |
| API read latency | `sandloom_http_request_duration_seconds`, reads | slower than 250 ms | `sandloom:sli_read_slow:ratio_rate*` |
| API write latency | writes | slower than 500 ms | `sandloom:sli_write_slow:ratio_rate*` |
| Login availability | `POST /auth/login` | `status` 5xx | `sandloom:sli_login_errors:ratio_rate*` |
| Login latency | `POST /auth/login` | slower than 2.5 s | `sandloom:sli_login_slow:ratio_rate*` |
| Event delivery | `sandloom_event_end_to_end_latency_seconds` | outbox commit → consumer commit slower than 5 s | `sandloom:sli_event_slow:ratio_rate*` |
| Outbox freshness | every rule evaluation (30 s) | oldest pending outbox row ≥ 10 s | `sandloom:sli_outbox_fresh:bool` (1 = good) |

Definitions and their limits:

- **Probes are not traffic.** `/health*`, `/ready`, `/metrics` and unmatched
  paths (`<unmatched>`, i.e. 404 scans) are excluded.
- **4xx are good events.** 401, 403, 404, 409, 422 and 429 are the API working
  as designed. A deployment that wants "rate limited" to count against it adds
  `status="429"` to the bad set.
- **Reads vs writes is the HTTP method.** Routes carry no class label (route
  policy is derived from the method, the authenticator and the declared risk;
  none of it is a metric label). For an SLO on one route (or a group of
  routes), add a `path=~"…"` matcher to the rules.
- **The latency histogram has no `status` label**: a fast 500 counts as fast.
  Read latency and availability together.
- **Thresholds must be bucket boundaries.** The HTTP histogram uses
  prometheus_client's default buckets (`… 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5 …`
  seconds); the event histogram `… 1, 2.5, 5, 10, 30, 60 …`. Pick an SLO
  threshold from those, or the SLI is interpolated guesswork.
- **Server-side only.** The app measures from its own socket: TLS, proxies,
  the gateway and the network are outside the SLI. An edge SLO needs the
  balancer's or a synthetic probe's metrics.
- **Event latency includes clock skew** between the producer's database
  (`emitted_at`) and the consumer host. Keep NTP on both.
- **Consumer lag has no ratio SLI.** Track it as a threshold instead:
  `sandloom_streams_group_oldest_unacked_age_seconds` (alert `SandloomConsumerGroupBehind`).

## Example SLOs

For a 2 vCPU monolith (2 Gunicorn workers, PostgreSQL on 2 vCPU), measured
over a rolling 30 days. "Load envelope" is the load under which the target was
met on the stand — above it, expect to miss the latency SLO first.

| SLO | Target | Load envelope (2 vCPU) | Evidence on the stand |
|---|---|---|---|
| API read availability | 99.9 % non-5xx | — | 0 errors in every valid read window |
| API read latency | 99 % < 250 ms | ≤ 500 reads/s | fixed 500/s: p50 5 ms, p99 193–232 ms; 650/s: p99 621–854 ms |
| API write availability | 99.9 % non-5xx | — | one baseline-build 200/s window answered 20 × 503 in 12 000 (0.17 %) — the budget is not theoretical |
| API write latency | 99 % < 500 ms | ≤ 200 writes/s | fixed 200/s: p50 14–20 ms, p99 126–211 ms; 290/s: p99 409–634 ms |
| Login availability | 99.9 % non-5xx | — | generic 401 for every wrong credential |
| Login latency | 99 % < 2.5 s | ≤ 5 logins/s | 8 concurrent: ≈ 9.2 logins/s, p99 1.08–1.36 s (argon2id m=64 MiB, t=3, p=4) |
| Outbox freshness | 99.9 % of evaluations < 10 s | ≤ 200 events/s | pending outbox rows peak ≈ 100 and return to 0 in every write-capacity window |
| Event delivery | 99 % < 5 s end-to-end | ≤ 200 events/s | 200/s: p99 0.67–0.77 s (one baseline round 4.5 s); 300/s: p99 30–39 s — **not sustainable** |
| Consumer lag | oldest unacked < 60 s, 99.9 % of the time | ≤ 200 events/s | lag returns to 0 after every window at 200/s |

Why these numbers and not tighter ones:

- **Reads**: p99 at 500/s sits at 190–230 ms, so 250 ms leaves little headroom;
  it is the tightest bucket boundary the stand met. Planned single-route reads
  and `/auth/me` saturate near 1 200/s, ordinary reads near 690/s — the
  envelope is ~70 % of the ordinary-read saturation point.
- **Writes**: at 200/s the p99 is ≤ 211 ms; 500 ms absorbs bursts towards
  290/s (where p99 already reaches 409–634 ms). Financial (`F`) writes
  saturate around 310–360/s, ordinary (`W`) around 470/s in closed loop.
- **Login** is dominated by the password hash on purpose. Its capacity
  (≈ 9/s on 2 vCPU) is a planning number: see `PASSWORD_HASH_PROFILE` in
  `docs/operations/operator-reference.md` → Password hashing. Do not "fix" a login SLO by
  weakening the hash.
- **Events**: the durable consumer's whole callback costs 13–15 ms under HTTP
  load; a pure drain without HTTP traffic reaches ≈ 510–520 events/s, but that
  is burst-drain capacity, not a sustained mixed rate. Between 200/s and
  300/s the consumer group starts to fall behind — set the envelope below the
  rate where `SandloomConsumerGroupBehind` would fire, and scale consumers (more
  processes) before raising it.

### Where the numbers come from

Oracle qualification of 2026-10-22 (`f898857a` vs `b609b5ee`), private
evidence `benchmarks/qualification/.state/oracle-main-20261022-f898857a/`:
four ARM Neoverse N1 vCPUs host, app limited to **2 vCPU / 1 GiB with two
Gunicorn workers**, PostgreSQL 16 on 2 vCPU / 1 GiB, separate cache, security
and broker Redis (AOF `appendfsync=always`), pool 5 + 5 per worker, strict
production RLS. The load generator ran on the same host without TLS, proxy or
network in the path, so production latencies are **higher** by your edge's
cost. Windows were 30–60 s and small in number; they establish orders of
magnitude, not confidence intervals.

## Error budget

The budget is what the SLO allows to go wrong: `1 − target` of the valid
events in the window.

| SLO | Target | Window | Budget | In time (full outage) | In events (at 100 req/s) |
|---|---|---|---|---|---|
| API read availability | 99.9 % | 30 d | 0.1 % | 43 min | 259 200 requests |
| API write availability | 99.9 % | 30 d | 0.1 % | 43 min | 259 200 requests |
| API read latency | 99 % | 30 d | 1 % | 7 h 12 min | 2 592 000 requests |
| Event delivery | 99 % | 30 d | 1 % | — | 1 % of events |
| *your SLO* | | | | | |

**Burn rate** = observed bad ratio ÷ budget. A burn rate of 1 spends exactly
the budget over the window; 14.4 spends 2 % of a 30-day budget per hour (gone
in ~2 days).

### Burn-rate alerts

`sandloom-slo.yml` ships multi-window, multi-burn-rate alerts (Google SRE workbook)
for read and write availability at the example 99.9 % target. A long window
proves the burn is real; the short one makes the alert resolve quickly once it
stops.

| Alert | Severity | Burn rate | Long / short window | Budget spent when it fires |
|---|---|---|---|---|
| `SandloomApi{Read,Write}AvailabilityBurnFast` | page | > 14.4 | 1h / 5m | 2 % |
| `SandloomApi{Read,Write}AvailabilityBurnSlow` | warning | > 6 | 6h / 30m | 5 % |

The latency, login and event SLIs are recorded but not alerted on by burn
rate: their thresholds depend more on your hardware than availability does.
Copy the availability pair, change the record name and budget (`0.01` for a
99 % SLO), and add a minimum-traffic guard
(`and sum by (job) (rate(sandloom_http_requests_total[1h])) > 1`) for low-traffic
services, where a handful of errors is a large ratio.

**Changing a target** touches three places together: the alert expressions in
`sandloom-slo.yml`, `slo()` in `deploy/observability/build_dashboards.py`
(regenerate the dashboards) and the table above. CI checks the SLO board and
the burn alerts agree on the budget.

### Error-budget policy (template)

Agree on it before the budget runs out; adapt the thresholds.

| Budget remaining (30 d) | Response |
|---|---|
| > 50 % | Ship normally. Spend budget on planned risk (migrations, upgrades). |
| 25–50 % | Ship, but no risky changes without a rollback plan; review the top error sources weekly. |
| 0–25 % | Freeze feature releases for the affected service; reliability work only. |
| exhausted | Freeze until the 30-day window recovers above 0; postmortem for every incident that spent > 20 % of the budget. |

Record for each SLO: the owner, the target and window, the dashboard link,
the alerts that protect it, and the date it was last reviewed (quarterly, or
after any capacity change).
