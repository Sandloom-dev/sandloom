---
search:
  exclude: true
title: "Cost of the tenant guard change of tenant_upsert (cachegrind A/B)"
---

# Cost of the tenant guard change of `tenant_upsert` (cachegrind A/B)

- **Date:** 2026-11-22
- **Task:** `tenant-upsert-helper` (PR #344), review round 2, CodeRabbit comment 4658372027
- **Question:** what does the guard change of this PR cost a guarded ORM write and a
  guarded ORM read, against `main`, and what does `tenant_upsert` itself cost?
- **Answer in one line:** the write path is 0.17% to 0.34% more expensive than `main`
  (2.8k to 5.8k instructions of 1.7M), the read path is not more expensive (it measures
  66k to 78k instructions cheaper, a difference this note does not explain and does not
  claim as a gain), and the `tenant_upsert` path got 46k to 57k instructions (0.8% to
  1.0%) cheaper with the early-outs of round 2.

## What changed in the guard

`src/sandloom/core/tenant.py` gains the upsert admission (`_tenant_upsert_admitted`,
the `_UPSERTS` marker, the `_UPSERT_SHAPES` cache) and, for every statement of a
`TenantScoped` model, an exact-type tenant comparison (`_is_bound_tenant`) in place of
`==`. The comment asked what that adds to the hot path of a statement that is not an
upsert. Round 2 added early-outs where the cheap test comes first:

- `_bound_tenant()` returns the context variable's value as it is when it is `None` or
  an exact `uuid.UUID` (the common case) and calls `_plain_uuid` only for a driver's
  subclass.
- `_adopt_bound_tenant` compares an exact `uuid.UUID` directly (one comparison) and
  walks the subclass checks (`int`, `bytes`) only for a subclass.
- `_flush_write_admitted` tests the flush window, the statement's shape and the reach
  before it reads the bound tenant, so a statement that is refused anyway does no
  tenant work first.

## Method

The stand is the kernel's cachegrind stand (`benchmarks/ladder/cg/`): the image
`sandloom-ladder-cg:3.13`, `valgrind --tool=cachegrind --cache-sim=no`,
`PYTHONHASHSEED=0`, and Ir/op = (Ir(K) - Ir(0)) / K with K = 200 operations after a
warm-up, one process per run, one fresh PostgreSQL database per run. The operations are
in `benchmarks/ladder/tenant_guard_rungs.py`, new in this PR (the model is defined in the
rung, the rows are seeded in the setup, which cancels in the difference):

| rung | one operation |
|---|---|
| `XTgW` | a new session as the bound tenant, `add` one row, `commit` (flush INSERT through `before_flush`, the mapper events and the cursor's flush-write check) |
| `XTgR` | a new session as the bound tenant, `select` one row by primary key (tenant criterion, loader criteria, the cursor's criterion check, the load stamp) |
| `XTgU` | `tenant_upsert` of one existing row as the bound tenant, then `commit` (only on a checkout that has the helper) |

Three trees, each run with the same command:

- `main` at `b116334c` (the rung file copied in; nothing else differs);
- the round-1 head `63b1532a`;
- the round-2 tree (this PR's head after the early-outs).

The stand's `cgdriver.py` runs `ladder.py`; the rungs have their own entry, so the run
used a scratch copy of `run_cg.sh` that mounts the tree under test and a copy of
`cgdriver.py` whose `LADDER` constant is `/w/benchmarks/ladder/tenant_guard_rungs.py`
(two lines of difference, not committed). `ladder.py` itself is untouched. The mode of
the collector is set with `LADDER_CG_GC`.

### Why the collector is frozen

The ladder's default mode lets the cyclic collector run, so the Ir of an operation
includes whichever collections fall inside the 200 operations, and where they fall
depends on the size of the heap the process had at start. Two builds with different
module sets (this PR adds functions and containers to `tenant.py`) have different heaps.
A first run in the default mode gave `XTgR` +46k against `main`; it was the phase of a
collection, not code: it vanished when the collector was frozen. So the comparison uses

- `freeze`: `gc.collect()` and `gc.freeze()` after the warm-up (the headline), and
- `off`: `freeze` and `gc.disable()` (a check that the headline is not an artifact of
  `freeze`).

Default-mode numbers of two trees are not comparable and are not recorded here.

## Results (Ir per operation, 2026-11-22)

### `LADDER_CG_GC=freeze` (headline)

| rung | `main` b116334c | round 1 63b1532a | round 2 | round 2 vs `main` | round 2 vs round 1 |
|---|---:|---:|---:|---:|---:|
| `XTgW` | 1,708,559 | 1,711,404 | 1,711,398 | +2,839 (+0.17%) | -6 (0.00%) |
| `XTgR` | 2,026,754 | 1,952,005 | 1,960,823 | -65,931 (-3.25%) | +8,818 (+0.45%) |
| `XTgU` | not available | 5,733,149 | 5,675,621 | not available | -57,528 (-1.00%) |

### `LADDER_CG_GC=off`

| rung | `main` b116334c | round 1 63b1532a | round 2 | round 2 vs `main` | round 2 vs round 1 |
|---|---:|---:|---:|---:|---:|
| `XTgW` | 1,704,779 | 1,706,683 | 1,710,599 | +5,820 (+0.34%) | +3,916 (+0.23%) |
| `XTgR` | 2,018,870 | 1,938,715 | 1,940,972 | -77,898 (-3.86%) | +2,257 (+0.12%) |
| `XTgU` | not available | 5,740,933 | 5,694,871 | not available | -46,062 (-0.80%) |

`main` has no `tenant_upsert`, so `XTgU` has no row for it.

## Reading the numbers

- **The write path (`XTgW`)**: +0.17% (freeze) to +0.34% (off) over `main`. The early-outs
  did not lower it (-6 in freeze, +3.9k in off). The cost is not attributed per
  function.
- **The upsert path (`XTgU`)**: the early-outs take 46k to 57k instructions (0.8% to
  1.0%) off an operation of 5.7M. `tenant_upsert` of one row costs 3.3 times a plain
  `add` and `commit` (5.7M against 1.7M): the helper builds an `INSERT ... ON CONFLICT
  DO UPDATE`, and the cursor rule rebuilds the statement to check it is the one the
  helper built (`ADR-0018`).
- **The read path (`XTgR`)**: no regression: round 2 is 66k to 78k lower than `main` in
  both modes, and 2k to 9k above round 1 (the early-outs are on the write side; this
  spread between rounds 1 and 2 is not attributed either).
- **The XTgR difference against `main` is unexplained.** What was ruled out: the Python
  eval loop differs by +1.5k instructions and the number of calls by +3 to +5 per
  operation (a `cProfile` count of both trees), so the guard does not do less work; the
  difference sits in C code that cachegrind reports as unsymbolized (`???:???`:
  CPython's static internals or a Cython extension). A bisect over the 25 hunks of the
  diff of `tenant.py` (main plus a subset of them, `off` mode) narrowed it to hunks 9 to
  14 (the `_flush_write_admitted` changes and the block that defines the upsert
  shapes); it does not follow the criterion path, the adoption helpers, the imports,
  the recorder refactor, or padding of the module, each tried on its own. One last
  probe shows how small a definition is enough to move this rung: `main` with an
  unused runtime-checkable `Protocol` and `NamedTuple` appended to `tenant.py` gives
  `XTgR` 2,001,478 (-17,392 against `main`'s 2,018,870), and with an unused `WeakSet`
  and `WeakKeyDictionary` appended 2,016,613 (-2,257). Definitions that no read ever
  calls move `XTgR` by that much, so a difference of this size on this rung is not
  evidence of work the guard saves; the whole 66k to 78k stays unexplained. This note
  does not claim it as a gain of the change: it takes the read path as unchanged, and
  `XTgW` and `XTgU` as the costs.

## Repeat it

```
LADDER_CG_GC=freeze CG_K=200 DATABASE_URL=postgresql+asyncpg://<user>:<password>@localhost:5433/<db> \
    sh <run_cg.sh scratch copy> XTgW XTgR XTgU
```

The test PostgreSQL of `deploy/compose/test.yml` (port 5433) and a Redis for the stand
(`BENCH_REDIS_URL`) are the resources. Run `main` and the tree under test one after the
other on an otherwise idle machine, once in `freeze` and once in `off`, and compare the
Ir/op of the same rung only.
