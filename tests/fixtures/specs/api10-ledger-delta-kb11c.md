# Ledger delta: KB-11c, measurement of the KB-11 trusted claims view (2026-11-09)

Measurement only: no guarantee, enforcing assertion or residual-risk
requirement changes, and no kernel code changes. KB-11 (PR #108, `f8e1bb7`;
follow-ups #111) put a trusted claims view with per-key `cpv` on every
verified access token. This file records what it costs, against the commit
before it.

## Verdict

**Budget (+1 % Ir per operation) is exceeded on the stand's claims-tier
rungs.** Request rungs `KRole` +3.4 %, `KPerm` +3.5 %, `KTen` +2.1 % (this one
within the stand's noise, below); the mint rungs +4.5 % / +5.3 %; a first
request of a token (fresh token) +17.5 %. Why, per rung, is under
"Where the instructions go". The PoC's +0.57 % for the whole map is not
reproduced on these rungs; the stand's denominator is small (no database, see
"What the stand is"), so the percentages overstate a request that also does
database work: the absolute delta per request is +18 k Ir (`KRole`).
M-lat is reported only (no threshold): p50 of a request is +6 % on this
machine, tails are not interpretable (load average 20 to 23 on 10 cores).

## Builds, image, stand

| item | value |
|---|---|
| base | `168f33ddc74d0a3ecbccdc4fbb30e8b5f5f9d81` (`f8e1bb7^`, `git worktree add --detach`) |
| KB-11 | `f8e1bb747e9dcd3aee8c08c0ff2f2750e5c23627` |
| candidate | `ee261084332c120cb8b57f5e852274eb486d6d73` (`origin/arch/kernel-boundary`), `src/` unchanged by this task |
| stand | `benchmarks/ladder/claims_rungs.py` (new, 13 rungs), installed by 4 lines in `ladder.py`; the same two files copied into the base checkout |
| cachegrind image | `sandloom-ladder-cg:3.13`, `cg/Dockerfile` + `cg/requirements.txt` unchanged; image id `sha256:21fe81618378fb1a61fd729f2bdf74503ca43fced0827af4d6f14fd263a50f93` (created 2026-11-08T10:50:24Z, arm64, 8 layers, top layer `4b3137d4cdf0`); valgrind 3.24.0, Python 3.13.16, orjson 3.13.0, pydantic 2.14.1, pydantic_core 2.50.1, PyJWT 2.15.1, fastapi 0.136.3, starlette 1.6.0 |
| image note | the tag is shared with other agents and is re-pointed by every `run_cg.sh` (each rebuilds it from the same unchanged Dockerfile); the id above is the tag at the end of the work, the versions above were read from it and from the first build (valgrind, Python) and agree |
| host | macOS 26.x arm64 (10 cores), docker 29.8.0 (aarch64 Linux VM); own `kb-11c-pg` (postgres:16) and `kb-11c-redis` (redis:7), the URLs are required by `run_cg.sh` and are never connected to by these rungs |

## What the stand is

`claims_rungs.py` builds one kernel process: a `CoreFacade` with a claims map
(`roles`, `permissions` from `bench.identity`, `tenant_ids` from
`bench.grants`, both local), the same map on both builds
(`build_claim_authorities` + `filter_to_composition` +
`_assign_claim_authorities`, which exist on both). Tokens are minted by the
production path (`collect_claims` + `create_access_token`), so the
candidate's carry `cpv` and the base's do not (747 against 547 bytes). Both
builds verify them with their own `decode_token` and both answer 200: nothing
is untrusted on either build.

- Request rungs drive a three-route FastAPI app straight through ASGI
  (`require(Role("admin"))`, `require(Perm("x.read"))`, `TenantScope()`), one
  token repeated (`KRole` `KPerm` `KTen`, the verified-token cache answers) or
  a fresh token every op (`KRoleC` `KTenC`).
- Micro rung: `decode_token` alone, cached (`KDec`) and fresh (`KDecC`).
- Mint rungs: `collect_claims` (`KMintC`), and with `create_access_token`
  (`KMint`).
- Parts, to attribute: `KLoads` (`orjson.loads` of the cached claims),
  `KCtx` (`UserContext(**claims)`), `KVerify` (signature and claims check of a
  fresh token), `KView` (`apply_view`, candidate only).
- The revocation `GET` goes to an in-memory cache, no database and no network:
  the numbers are the authorization's own cost, and the denominator is about
  0.5 M Ir per request, against 3.7 M for an S5 request with a database read
  (`STANDS.md` section D, other build, quoted as scale only). Over a database
  request the same +18 k is about +0.5 %.
- Instructions are Ir(K) - Ir(0) over K ops, `LADDER_CG_GC=freeze`
  (production's `GC_FREEZE_AFTER_STARTUP`, `STANDS.md` section D),
  `PYTHONHASHSEED=0`. Fresh tokens are minted in the setup (K + 40), so the
  setup cancels.

## M-ir: instructions per operation

Base and candidate run in parallel in two containers of the same image, one
checkout each (Ir does not depend on load). Run 1 used K = 200, runs 2 and 3
K = 1000 (run 3 repeats run 2 unchanged). The delta uses the mean of runs 2
and 3; run 1 is shown as a cross-check.

| rung | what it measures | base run 2 | base run 3 | cand run 2 | cand run 3 | base mean | cand mean | delta Ir | delta % | run 1 (K=200) delta % |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `KRole` | request, `Role`, token cached | 532,791 | 532,803 | 550,679 | 551,140 | 532,797 | 550,910 | +18,112 | **+3.40** | +2.31 |
| `KPerm` | request, `Perm`, token cached | 534,901 | 535,075 | 553,070 | 554,640 | 534,988 | 553,855 | +18,867 | **+3.53** | +4.13 |
| `KTen` | request, `TenantScope`, token cached | 442,852 | 447,110 | 454,796 | 454,049 | 444,981 | 454,422 | +9,442 | **+2.12** | +0.95 |
| `KRoleC` | request, `Role`, fresh token | 1,133,870 | 1,132,315 | 1,333,032 | 1,330,527 | 1,133,092 | 1,331,780 | +198,687 | **+17.53** | +17.63 |
| `KTenC` | request, `TenantScope`, fresh token | 1,060,018 | 1,069,426 | 1,252,299 | 1,250,127 | 1,064,722 | 1,251,213 | +186,491 | **+17.52** | +17.53 |
| `KDec` | micro: `decode_token`, cached | 22,958 | 22,205 | 33,752 | 33,670 | 22,582 | 33,711 | +11,130 | **+49.29** | +9.35 |
| `KDecC` | micro: `decode_token`, fresh | 630,661 | 619,102 | 818,448 | 818,367 | 624,882 | 818,408 | +193,526 | **+30.97** | +26.90 |
| `KMintC` | mint: `collect_claims` | 850,157 | 849,012 | 887,741 | 888,301 | 849,584 | 888,021 | +38,436 | **+4.52** | +5.00 |
| `KMint` | mint: `collect_claims` + `create_access_token` | 1,171,173 | 1,168,944 | 1,231,969 | 1,230,928 | 1,170,058 | 1,231,448 | +61,390 | **+5.25** | +4.83 |

Noise of the stand: the same build, run 2 against run 3, differs by at most
0.3 % on `KRole`, `KPerm`, `KRoleC`, `KMintC`, `KMint` and the candidate's
`KTen`, `KTenC`, `KDec`, `KDecC`; the base differs more on `KTen` (+1.0 %:
442,852 against 447,110), `KTenC` (+0.9 %), `KDec` (-3.3 %) and `KDecC`
(-1.8 %). So `KTen`'s +2.1 % carries about +/-1 point; the `KDec` / `KDecC`
percentages are indicative only (the micro rung is 23 k Ir, and also varies by
1 to 5 k between K = 200 and K = 1000); at K = 20 000 (run 4 below) `KDec` is
stable and shows +10.3 k. Every other row is far outside its noise.

Parts (run 4: K = 20 000; run 5: K = 1000; same two builds):

| rung | base | cand | delta Ir |
|---|---:|---:|---:|
| `KDec` (K = 20 000) | 22,610 | 32,935 | +10,325 |
| `KLoads` (K = 20 000) | 14,517 | 20,636 | +6,119 |
| `KCtx` (K = 20 000) | 59,581 | 66,004 | +6,423 |
| `KVerify` (K = 1000) | 618,818 | 758,260 | +139,442 |
| `KView` (K = 1000, candidate only) |  | 54,928 | +54,928 |

## Where the instructions go

Python call counts per op (cProfile, same stand, base against candidate) and
the part rungs above:

- **Request, token cached (`KRole` +18.1 k, `KPerm` +18.9 k, `KTen` +9.4 k).**
  The cached decode costs +10.3 k (`KDec`): +6.1 k is `orjson.loads` of the
  cached claims, which now carry `cpv` (+200 bytes, 5 digests: `roles`,
  `permissions`, `tenant_ids` and the issuer's `email`, `email_verified`);
  the other +4.2 k is the view's per-hit check: three new calls
  (`_claim_policy`, `get_active_core`, the `claim_authorities` property) and
  the policy marker in the cache key (about ten more Python calls per request
  in all). `UserContext(**claims)` costs +6.4 k (`KCtx`): the
  `untrusted_claims` field and its dict, and `cpv` copied into the claims.
  The rest (about 1.4 k) is not attributed. `KTen` is lower because
  `TenantScope` reads `bearer_claims` and builds no `UserContext`: it carries
  the +10 k decode and not the +6.4 k `KCtx`.
- **First request of a token (`KRoleC`, `KTenC` +17.5 %, `KDecC` +31 %,
  +194 k).** Two parts: `KVerify` +139 k and `KView` +55 k. `KVerify` is
  PyJWT's verification of a token 200 bytes larger: `jwt/api_jws.py:<genexpr>`
  (the per-byte base64 check) runs 748 times per decode against 548, about
  700 instructions per added byte. `KView` is `apply_view` (once per verified
  token, cached after). Every token a client keeps for its lifetime pays this
  once per process that sees it; a token seen by N replicas pays N times.
- **Mint (`KMintC` +38.4 k, `KMint` +61.4 k).** The merge itself stamps `cpv`
  from digests computed once at startup (`dict(authorities.digests)`), which
  is cheap. The +38 k is the authority binding that every local claims answer
  now carries through the contract wire (`contract_wire.encode` /
  `decode_untyped` +4 calls each, `_local_answer`, +28 `isinstance`). The
  other +23 k of `KMint` (by subtraction) is `create_access_token` encoding
  and signing the 200 bytes larger payload.

No code is changed by this task. Candidate levers, not evaluated here: keep
`cpv` off the wire for keys the verifier needs no digest of, a shorter digest
or one digest per authority rather than per key, parse the cached view once
per request, strip `cpv` from the cached claims, skip the contract wire for a
local claims answer.

## M-lat: latency, reported only

Base and candidate alternate in one session: 12 rounds, one fresh process per
build per rung per round (order flips every round), 3 000 timed ops after 300
warm-up ops, `gc.freeze()` after warm-up, `time.perf_counter_ns` per op,
percentiles per process, then the median over the 12 rounds. Both builds run
the interpreter of the candidate checkout's venv with their own `src/` on
`PYTHONPATH`. The machine was loaded by other agents (load average 20 to 23 on
10 cores): p50 repeated within a few percent between rounds; **p95 and p99 are
not interpretable here** (one build's single round reaches 3 to 6 ms on a
~0.15 ms op, in either build). No threshold was set.

| rung | pct | base median ns | cand median ns | delta % (medians) | paired delta median % | paired min..max % | base min..max ns |
|---|---|---:|---:|---:|---:|---:|---|
| `KRole` | p50 | 38,562 | 41,312 | +7.1 | +6.0 | +3..+10 | 37,250..40,958 |
| `KRole` | p95 | 41,562 | 44,500 | +7.1 | +6.3 | -73..+21 | 38,584..171,583 |
| `KRole` | p99 | 45,375 | 65,188 | +43.7 | +16.0 | -72..+218 | 40,375..579,750 |
| `KPerm` | p50 | 37,875 | 40,813 | +7.8 | +6.7 | +5..+10 | 37,041..40,292 |
| `KPerm` | p95 | 41,708 | 44,250 | +6.1 | +6.5 | -3..+15 | 38,333..52,250 |
| `KPerm` | p99 | 45,292 | 47,959 | +5.9 | +6.4 | -62..+21 | 39,583..199,000 |
| `KTen` | p50 | 33,479 | 35,104 | +4.9 | +5.1 | +3..+6 | 32,791..35,917 |
| `KTen` | p95 | 37,312 | 39,292 | +5.3 | +8.0 | -19..+14 | 34,709..44,417 |
| `KTen` | p99 | 48,229 | 60,250 | +24.9 | +17.7 | -64..+138 | 37,833..138,542 |
| `KDec` | p50 | 1,354 | 1,917 | +41.5 | +40.0 | +32..+47 | 1,292..1,417 |
| `KDec` | p95 | 1,458 | 2,021 | +38.6 | +38.3 | +27..+49 | 1,375..1,542 |
| `KDec` | p99 | 1,542 | 2,104 | +36.5 | +38.4 | -33..+72 | 1,458..3,125 |
| `KMint` | p50 | 151,146 | 161,500 | +6.9 | +3.8 | -39..+575 | 145,792..780,666 |
| `KMint` | p95 | 161,167 | 271,062 | +68.2 | +21.8 | -51..+93 | 150,833..2,877,000 |
| `KMint` | p99 | 224,250 | 370,688 | +65.3 | +21.4 | -53..+389 | 162,334..6,750,833 |

Reading: the p50 of a request grows by about 2.7 µs (+5 to +8 %, in every
round), of the cached decode by about 0.56 µs (+40 %), of a mint by about
10 µs (+4 to +7 % at p50). Tails (p95, p99) are scheduler noise on this host.

## Commands

```bash
# own resources (ports 23411, 23412), removed at the end
docker run -d --name kb-11c-pg -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_password \
  -e POSTGRES_DB=sandloom_test -p 127.0.0.1:23411:5432 postgres:16 -c max_connections=600
docker run -d --name kb-11c-redis -p 127.0.0.1:23412:6379 redis:7-alpine redis-server --databases 256
git worktree add --detach <scratch>/base f8e1bb7^   # then copy ladder/claims_rungs.py and ladder/ladder.py into it
export DATABASE_URL=postgresql+asyncpg://test_user:test_password@127.0.0.1:23411/sandloom_test
export CACHE_URL=redis://127.0.0.1:23412/0 BROKER_URL=redis://127.0.0.1:23412/1
export LADDER_CG_GC=freeze
# per checkout (its own run_cg.sh), base and candidate in parallel
CG_K=1000 benchmarks/ladder/cg/run_cg.sh KRole KPerm KTen KRoleC KTenC KDec KDecC KMintC KMint
CG_K=20000 benchmarks/ladder/cg/run_cg.sh KLoads KCtx KDec
CG_K=1000 benchmarks/ladder/cg/run_cg.sh KVerify KView      # KView on the candidate only
# latency: alternating processes (scratch script), 12 rounds
python lat_all.py <base> <cand> 12 3000 300 lat.json KRole KPerm KTen KDec KMint
```

`lat_all.py`, `lat.py` (one build, one rung, per-op `perf_counter_ns`) and the
`cProfile` call-count diff are scratch scripts, not part of the stand.
