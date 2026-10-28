"""Render the ladder results (ladder/results/*.jsonl + cg.json) as Markdown.

uv run python ladder/render.py > /tmp/tables.md
"""

from __future__ import annotations

import json
from pathlib import Path

RES = Path(__file__).resolve().parent / "results"


def load(phase: str, key: str = "rung") -> dict[str, dict]:
    path = RES / f"{phase}.jsonl"
    out: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            out[rec[key] if key else len(out)] = rec  # last record wins
    return out


def cg() -> dict[str, float]:
    path = RES / "cg.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def f(v, fmt="{:,.0f}"):
    return "–" if v is None else fmt.format(v)


def main() -> None:
    cpu, rt, al, mem, calls = (
        load(p) for p in ("cpu", "rt", "alloc", "memray", "calls")
    )
    ins = cg()
    rungs = list(dict.fromkeys([*cpu, *rt, *mem, *ins]))
    rungs = [r for r in rungs if not r.startswith("L6-")]

    print(
        "### Main table (sequential, one connection, asyncio loop; CPU/wall columns are conc-1 and NOT reliable on this host — caveat 1)\n"
    )
    print(
        "| rung | what | CPU µs/op (min–max) | wall µs | wall−CPU µs | PG RT/op | instr/op | allocs/op | py calls/op | cyclic garbage obj/op | gen0 GC /1k ops |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rungs:
        c, t, a, m, k = (
            cpu.get(r, {}),
            rt.get(r, {}),
            al.get(r, {}),
            mem.get(r, {}),
            calls.get(r, {}),
        )
        desc = (c or t or m or k or {"desc": ""}).get("desc", "")
        rng = (
            f"{f(c.get('cpu_us'))} ({f(c.get('cpu_us_min'))}–{f(c.get('cpu_us_max'))})"
            if c
            else "–"
        )
        print(
            f"| {r} | {desc} | {rng} | {f(c.get('wall_us'))} | {f(c.get('wait_us'))} | "
            f"{f(t.get('rt_per_op'), '{:g}')} | {f(ins.get(r))} | {f(m.get('allocs_per_op'))} | "
            f"{f(k.get('calls_per_op'))} | {f(a.get('cyclic_garbage_objects_per_op'), '{:.2f}')} | "
            f"{f(c.get('gen0_per_1k'), '{:.1f}')} |"
        )

    pairs = [
        ("L0", "L0t", "explicit transaction (BEGIN/COMMIT)"),
        ("L0t", "L2t", "SQLAlchemy Core (both in BEGIN/COMMIT)"),
        ("L2t", "L3t", "ORM session.get -> instance (both in BEGIN/COMMIT)"),
        ("L3t", "L4t", "pydantic ItemOut + model_dump_json"),
        ("L4t", "L5", "Sandloom engine + db_session() UoW"),
        (
            "L5",
            "L6",
            "full HTTP: FastAPI/Starlette + 10 Sandloom middlewares (in-process ASGI)",
        ),
        ("L6", "L7A", "combined fast path (Variant A): no tx, no ORM, no pydantic"),
        ("L6", "L6B", "Variant B: same endpoint, DB_LAZY_BEGIN=true"),
        (
            "L6",
            "L6R",
            "declared ReadOnlyDbSession (same handler), DB_READ_ONLY_SESSIONS=true",
        ),
    ]
    import statistics as _st

    c64: dict[str, list[float]] = {}
    for rec in load_all("chain64"):
        c64.setdefault(rec["rung"], []).extend(rec["cpu_all"])
    cpu64 = {k: {"cpu_us": _st.median(v)} for k, v in c64.items()}
    print("\n### Required pairs (B − A)\n")
    print(
        "CPU = interleaved conc-64 asyncio medians (chain64); instr = cachegrind; "
        "allocs = memray total allocations per op (incl. freed); RT = wire proxy.\n"
    )
    print(
        "| A → B | layer added | CPU µs A → B | Δ CPU µs | Δ temp allocs/op | Δ instr/op | Δ RT |"
    )
    print("|---|---|---|---|---|---|---|")
    for a_, b_, what in pairs:
        ca, cb = cpu64.get(a_, {}).get("cpu_us"), cpu64.get(b_, {}).get("cpu_us")
        ma, mb = (
            mem.get(a_, {}).get("allocs_per_op"),
            mem.get(b_, {}).get("allocs_per_op"),
        )
        ra, rb = rt.get(a_, {}).get("rt_per_op"), rt.get(b_, {}).get("rt_per_op")
        ia, ib = ins.get(a_), ins.get(b_)
        sub = lambda x, y: None if x is None or y is None else y - x  # noqa: E731
        print(
            f"| {a_} → {b_} | {what} | {f(ca)} → {f(cb)} | {f(sub(ca, cb), '{:+,.0f}')} | "
            f"{f(sub(ma, mb), '{:+,.0f}')} | {f(sub(ia, ib), '{:+,.0f}')} | {f(sub(ra, rb), '{:+g}')} |"
        )

    chain = [
        ("L0", "L1"),
        ("L0", "L0t"),
        ("L0", "L2a"),
        ("L2a", "L2c"),
        ("L2c", "L2"),
        ("L2", "L3"),
        ("L3c", "L3"),
        ("L3", "L3t"),
        ("L3t", "L3g"),
        ("L3", "L4"),
        ("L4o", "L4"),
        ("L4", "L5e"),
        ("L5e", "L5"),
        ("L5", "L6r"),
        ("L6r", "L6"),
        ("L6fr", "L6fa"),
        ("L6fa", "L6"),
        ("L7a", "L6"),
        ("L7u", "L6"),
        ("L7p", "L7a"),
    ]
    print("\n### Increments (B − A)\n")
    print("| A → B | Δ CPU µs | Δ instr | Δ allocs | Δ py calls | Δ RT |")
    print("|---|---|---|---|---|---|")
    for a_, b_ in chain:

        def d(src, key, a_=a_, b_=b_):
            x, y = src.get(a_, {}), src.get(b_, {})
            if key in x and key in y:
                return y[key] - x[key]
            return None

        di = (ins[b_] - ins[a_]) if a_ in ins and b_ in ins else None
        print(
            f"| {a_} → {b_} | {f(d(cpu, 'cpu_us'), '{:+,.0f}')} | {f(di, '{:+,.0f}')} | "
            f"{f(d(mem, 'allocs_per_op'), '{:+,.0f}')} | {f(d(calls, 'calls_per_op'), '{:+,.0f}')} | "
            f"{f(d(rt, 'rt_per_op'), '{:+g}')} |"
        )

    mwi = {r[3:]: ins[r] for r in ins if r.startswith("L6-")}
    mwm = load("mwmemray")
    if mwi or mwm:
        print("\n### Per-middleware cost on GET /s3 (L6 − L6-without-X)\n")
        print("| middleware | instr/op | allocs/op |")
        print("|---|---|---|")
        for name in dict.fromkeys([*mwi, *(k[3:] for k in mwm)]):
            di = ins["L6"] - mwi[name] if name in mwi and "L6" in ins else None
            dm = (
                mem["L6"]["allocs_per_op"] - mwm[f"L6-{name}"]["allocs_per_op"]
                if f"L6-{name}" in mwm and "L6" in mem
                else None
            )
            print(f"| {name} | {f(di, '{:+,.0f}')} | {f(dm, '{:+,.0f}')} |")

    for phase, title in (
        ("cpu64", "Concurrency 64 (in-process, asyncio unless noted)"),
        ("cpuuv", "uvloop, sequential"),
    ):
        data = load(phase)
        if not data:
            continue
        print(f"\n### {title}\n")
        print("| rung | CPU µs/op (min–max) | wall µs/op | gen0 GC /1k |")
        print("|---|---|---|---|")
        for r, c in data.items():
            print(
                f"| {r} | {f(c['cpu_us'])} ({f(c['cpu_us_min'])}–{f(c['cpu_us_max'])}) | "
                f"{f(c['wall_us'])} | {f(c['gen0_per_1k'], '{:.1f}')} |"
            )

    sock = RES / "socket.jsonl"
    if sock.exists():
        print("\n### Real socket: uvicorn, 1 worker; server-process CPU per request\n")
        print(
            "| app | path | loop / http | conc | relay | server CPU µs/req (min–max) | wall µs/req | rps |"
        )
        print("|---|---|---|---|---|---|---|---|")
        for line in sock.read_text().splitlines():
            s = json.loads(line)
            print(
                f"| {s['app']} | {s['path']} | {s['loop']} / {s['http']} | {s['conc']} | {s['relay']} | "
                f"{f(s['server_cpu_us'])} ({f(s['cpu_min'])}–{f(s['cpu_max'])}) | {f(s['wall_us'])} | {f(s['rps'])} |"
            )

    print("\n### Allocations per op by layer (memray, innermost Python frame)\n")
    for r in ("L0", "L2", "L3", "L4", "L5", "L6", "L7a"):
        if r in mem:
            layers = ", ".join(
                f"{k} {v:g}" for k, v in list(mem[r]["layers"].items())[:10]
            )
            print(f"- **{r}** ({mem[r]['allocs_per_op']:,.0f}/op): {layers}")

    print("\n### Top 15 allocation sites per op (memray)\n")
    for r in ("L0", "L2", "L3", "L4", "L5", "L6"):
        if r not in mem:
            continue
        print(f"\n**{r}** — {mem[r]['desc']}\n")
        print("| site | allocs/op |")
        print("|---|---|")
        for site, n in mem[r]["top"]:
            print(f"| `{site}` | {n:g} |")

    print(
        "\n### Retained with GC disabled (tracemalloc; cyclic garbage / caches), top sites\n"
    )
    for r in ("L3", "L5", "L6"):
        if r not in al:
            continue
        a = al[r]
        print(
            f"\n**{r}**: retained blocks/op {a['blocks_retained_gc_off_per_op']:.2f}, "
            f"gen0 counter growth/op {a['gen0_count_growth_per_op']:.2f}, "
            f"unreachable (cyclic) objects/op {a['cyclic_garbage_objects_per_op']:.2f}\n"
        )
        print("| site | blocks/op | bytes/op |")
        print("|---|---|---|")
        for site, n, b in a["tracemalloc_top"][:10]:
            print(f"| `{site}` | {n:g} | {b} |")

    print("\n### Python function calls per op by layer (cProfile)\n")
    for r in ("L0", "L2", "L3", "L4", "L5", "L6"):
        if r in calls:
            layers = ", ".join(
                f"{k} {v:g}" for k, v in list(calls[r]["layers"].items())[:10]
            )
            print(f"- **{r}** ({calls[r]['calls_per_op']:,.0f}/op): {layers}")


def load_all(phase: str) -> list[dict]:
    path = RES / f"{phase}.jsonl"
    return (
        [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    )


def chain_tables() -> None:
    import statistics as st

    for phase, title in (
        ("chain", "asyncio, sequential (conc 1)"),
        ("chain64", "asyncio, conc 64"),
        ("chainuv", "uvloop, sequential (conc 1)"),
        ("chainuv64", "uvloop, conc 64"),
    ):
        recs = load_all(phase)
        if not recs:
            continue
        by: dict[str, list[dict]] = {}
        for r in recs:
            by.setdefault(r["rung"], []).append(r)
        print(f"\n### Interleaved chain — {title}\n")
        print(
            "| rung | CPU µs/op median (min–max over rounds×reps) | user µs | sys µs | wall µs | runs |"
        )
        print("|---|---|---|---|---|---|")
        for rung, rs in by.items():
            allc = [c for r in rs for c in r["cpu_all"]]
            print(
                f"| {rung} | {st.median(allc):,.0f} ({min(allc):,.0f}–{max(allc):,.0f}) | "
                f"{st.median(r['user_us'] for r in rs):,.0f} | {st.median(r['sys_us'] for r in rs):,.0f} | "
                f"{st.median(r['wall_us'] for r in rs):,.0f} | {len(allc)} |"
            )


def prof_diff(a: str, b: str, top: int = 15) -> None:
    prof = load("prof")
    if a not in prof or b not in prof:
        return
    fa = {f[0]: f for f in prof[a]["funcs"]}
    fb = {f[0]: f for f in prof[b]["funcs"]}
    rows = []
    for name in set(fa) | set(fb):
        ta = fa.get(name, [0, 0, 0, 0])
        tb = fb.get(name, [0, 0, 0, 0])
        rows.append((tb[2] - ta[2], tb[1] - ta[1], name))
    rows.sort(key=lambda r: -abs(r[0]))
    print(
        f"\n**cProfile own-time diff {a} → {b}** (profiled totals "
        f"{prof[a]['prof_total_us']:,.0f} → {prof[b]['prof_total_us']:,.0f} µs/op; "
        "cProfile inflates absolute time, use for ranking)\n"
    )
    print("| Δ own µs/op | Δ calls/op | function |")
    print("|---|---|---|")
    for dt, dc, name in rows[:top]:
        print(f"| {dt:+.1f} | {dc:+.1f} | `{name}` |")


def socket_latency() -> None:
    for phase, title in (
        ("socket2", "Real socket, uvicorn uvloop+httptools, 1 worker"),
    ):
        recs = load_all(phase)
        if recs:
            print(f"\n### {title}\n")
            print(
                "| app | path | env | conc | server CPU µs/req (min–max) | wall µs/req | rps |"
            )
            print("|---|---|---|---|---|---|---|")
            for s in recs:
                print(
                    f"| {s['app']} | {s['path']} | {' '.join(s.get('env') or [])} | {s['conc']} | "
                    f"{s['server_cpu_us']:,.0f} ({s['cpu_min']:,.0f}–{s['cpu_max']:,.0f}) | "
                    f"{s['wall_us']:,.0f} | {s['rps']:,.0f} |"
                )
    recs = load_all("latency")
    if recs:
        print(
            "\n### Open-loop latency at a fixed arrival rate (CO-corrected, 30 s after 5 s warm-up)\n"
        )
        print(
            "| rate rps | app | path | env | achieved | p50 ms | p90 ms | p99 ms | p99.9 ms | max ms | server CPU µs/req |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        for s in recs:
            print(
                f"| {s['rate']:.0f} | {s['app']} | {s['path']} | {' '.join(s.get('env') or [])} | "
                f"{s['achieved_rps']:,.0f} | {s['p50_ms']:.2f} | {s['p90_ms']:.2f} | {s['p99_ms']:.2f} | "
                f"{s['p999_ms']:.2f} | {s['max_ms']:.1f} | {s['server_cpu_us']:,.0f} |"
            )


if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) > 1 and _sys.argv[1] == "extra":
        chain_tables()
        socket_latency()
        for pair in (
            ("L0", "L0t"),
            ("L6B", "L6"),
            ("L5B", "L5"),
            ("L6R", "L6"),
            ("L6fa", "L6"),
            ("L6fr", "L6r"),
            ("L6r", "L6"),
        ):
            prof_diff(*pair)
        raise SystemExit(0)

    main()
