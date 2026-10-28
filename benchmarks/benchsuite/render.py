"""Render benchmark JSON as Markdown (CI step summary, or locally).

Inputs may be result JSON files written by ``benchsuite.run --json-out``, a
JSON list of such documents, or raw CI logs containing ``BENCHJSON`` lines
(e.g. ``gh run view <id> --log > run.log``) — chunks are reassembled per label.

    python -m benchsuite.render results.json
    gh run view 123456 --log | python -m benchsuite.render -
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_CHUNK_RE = re.compile(r"BENCHJSON (\S+) (\d+)/(\d+) (.*)$")
SCENARIO_TITLES = {
    "s1": "S1 trivial GET",
    "s2": "S2 authenticated GET (JWT + 1 Redis GET)",
    "s3": "S3 DB read (indexed SELECT by id)",
    "s4": "S4 write + outbox event (2 INSERTs, 1 tx)",
}
IMPL_ORDER = ["sandloom", "fastapi", "litestar", "litestar-asyncpg"]


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def docs_from_text(text: str) -> list[dict[str, Any]]:
    """Documents from a JSON text, or from ``BENCHJSON`` log lines."""
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    chunks: dict[str, dict[int, str]] = defaultdict(dict)
    totals: dict[str, int] = {}
    for line in text.splitlines():
        m = _CHUNK_RE.search(line)
        if not m:
            continue
        label, idx, total, payload = (
            m.group(1),
            int(m.group(2)),
            int(m.group(3)),
            m.group(4),
        )
        chunks[label][idx] = payload
        totals[label] = total
    docs = []
    for label, parts in chunks.items():
        if len(parts) != totals[label]:
            print(f"warning: incomplete BENCHJSON for {label}", file=sys.stderr)
            continue
        docs.append(json.loads("".join(parts[i] for i in sorted(parts))))
    return docs


def load(paths: Iterable[str]) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for path in paths:
        text = sys.stdin.read() if path == "-" else Path(path).read_text()
        docs.extend(docs_from_text(text))
    return docs


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def _median(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


#: Closed-loop generators, in order of preference for the headline table.
CLOSED_LOOP = ("wrk", "oha")
#: oha vs wrk rps disagreement that gets flagged in the cross-check.
CROSSCHECK_TOLERANCE = 0.10

Key = tuple[Any, ...]  # (python, workers, server, impl, scenario, loadgen)


def aggregate(docs: list[dict[str, Any]]) -> dict[Key, dict[str, Any]]:
    """(python, workers, server, impl, scenario, loadgen) -> median-of-reps row.

    Rows without a ``loadgen`` field (documents from before the field existed)
    were measured with oha.
    """
    groups: dict[Key, list[dict[str, Any]]] = defaultdict(list)
    for doc in docs:
        py = doc.get("meta", {}).get("python", "?")
        for row in doc.get("results", []):
            key = (
                py,
                row["workers"],
                row["server"],
                row["impl"],
                row["scenario"],
                row.get("loadgen", "oha"),
            )
            groups[key].append(row)
    out = {}
    for key, rows in groups.items():
        rps = [r["rps"] for r in rows]
        out[key] = {
            "reps": len(rows),
            "rps": _median(rps),
            "rps_min": min(rps),
            "rps_max": max(rps),
            "target_rps": _median([r.get("target_rps") for r in rows]),
            "p50_ms": _median([r.get("p50_ms") for r in rows]),
            "p90_ms": _median([r.get("p90_ms") for r in rows]),
            "p99_ms": _median([r.get("p99_ms") for r in rows]),
            "p999_ms": _median([r.get("p999_ms") for r in rows]),
            "errors": sum(r.get("errors", 0) for r in rows),
            "cpu_us_per_req": _median([r.get("cpu_us_per_req") for r in rows]),
            "rss_mb": _median([r.get("rss_mb") for r in rows]),
            "s4_outbox_ok": all(r.get("s4_outbox_ok", True) for r in rows),
        }
    return out


def _fmt(v: float | None, digits: int = 1) -> str:
    return "–" if v is None else f"{v:,.{digits}f}"


def _impl_rank(name: str) -> int:
    return IMPL_ORDER.index(name) if name in IMPL_ORDER else len(IMPL_ORDER)


def _ratio(a: float | None, b: float | None) -> str:
    if not a or not b:
        return "–"
    return f"{a / b:.2f}×"


def _sort_key(k: Key) -> tuple[Any, ...]:
    return (k[0], k[1], k[2], _impl_rank(k[3]), k[5])


def crosscheck(agg: dict[Key, dict[str, Any]]) -> list[dict[str, Any]]:
    """oha vs wrk rps for every row measured by both (same job)."""
    out = []
    for k, r in agg.items():
        if k[5] != "oha":
            continue
        w = agg.get((*k[:5], "wrk"))
        if not w or not r["rps"] or not w["rps"]:
            continue
        diff = (w["rps"] - r["rps"]) / max(w["rps"], r["rps"])
        out.append(
            {
                "key": k[:5],
                "oha": r["rps"],
                "wrk": w["rps"],
                "diff": diff,
                "flag": abs(diff) > CROSSCHECK_TOLERANCE,
            }
        )
    return sorted(out, key=lambda c: _sort_key((*c["key"], "")))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _headline(agg: dict[Key, dict[str, Any]]) -> list[str]:
    """Sandloom ÷ FastAPI per (python, workers, server), closed loop."""
    scenarios = sorted({k[4] for k in agg if k[5] in CLOSED_LOOP})
    combos = sorted({(k[0], k[1], k[2], k[5]) for k in agg if k[5] in CLOSED_LOOP})
    rows = []
    for py, w, server, gen in combos:
        cells, any_pair = [], False
        for s in scenarios:
            a = agg.get((py, w, server, "sandloom", s, gen))
            f = agg.get((py, w, server, "fastapi", s, gen))
            if a and f:
                any_pair = True
                cells.append(
                    f"**{_ratio(a['rps'], f['rps'])}** / "
                    f"{_ratio(a['cpu_us_per_req'], f['cpu_us_per_req'])}"
                )
            else:
                cells.append("–")
        if any_pair:
            rows.append(
                f"| {py} | {w} | {server} | {gen} | " + " | ".join(cells) + " |"
            )
    if not rows:
        return []
    return [
        "### Sandloom relative to bare FastAPI (same server, same job, closed loop)",
        "",
        "Each cell: **rps ratio** (Sandloom ÷ FastAPI, higher is better) / "
        "CPU-per-request ratio (lower is better).",
        "",
        "| python | workers | server | loadgen | "
        + " | ".join(s.upper() for s in scenarios)
        + " |",
        "|---|---|---|---|" + "---|" * len(scenarios),
        *rows,
        "",
    ]


def _closed_tables(agg: dict[Key, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for s in sorted({k[4] for k in agg if k[5] in CLOSED_LOOP}):
        lines += [
            f"### {SCENARIO_TITLES.get(s, s)} — max throughput (closed loop)",
            "",
            "| python | workers | server | impl | loadgen | rps | rps range | p50 ms "
            "| p90 ms | p99 ms | errors | CPU µs/req | RSS MB |",
            "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        keys = sorted(
            (k for k in agg if k[4] == s and k[5] in CLOSED_LOOP), key=_sort_key
        )
        for k in keys:
            r = agg[k]
            flag = "" if r["s4_outbox_ok"] else " ⚠ outbox≠rows"
            lines.append(
                f"| {k[0]} | {k[1]} | {k[2]} | {k[3]}{flag} | {k[5]} | "
                f"{_fmt(r['rps'])} | {_fmt(r['rps_min'], 0)}–{_fmt(r['rps_max'], 0)} | "
                f"{_fmt(r['p50_ms'], 2)} | {_fmt(r['p90_ms'], 2)} | "
                f"{_fmt(r['p99_ms'], 2)} | {r['errors']} | "
                f"{_fmt(r['cpu_us_per_req'])} | {_fmt(r['rss_mb'], 0)} |"
            )
        lines.append("")
    return lines


def _open_table(agg: dict[Key, dict[str, Any]]) -> list[str]:
    keys = sorted(
        (k for k in agg if k[5] == "wrk2"), key=lambda k: (k[4], *_sort_key(k))
    )
    if not keys:
        return []
    lines = [
        "### Latency at a fixed rate (wrk2, open loop, coordinated-omission corrected)",
        "",
        "Target rate = a fraction of the closed-loop max measured in the same job "
        "(or the `rate` input). Latency is measured from the intended send time, "
        "so a server that falls behind shows it in the tail. If achieved < target "
        "the server could not sustain the rate (its latency is then unbounded).",
        "",
        "| scenario | python | workers | server | impl | target rps | achieved rps "
        "| p50 ms | p90 ms | p99 ms | p99.9 ms | errors | CPU µs/req |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for k in keys:
        r = agg[k]
        behind = (
            " ⚠"
            if r["target_rps"] and r["rps"] and r["rps"] < 0.97 * r["target_rps"]
            else ""
        )
        flag = "" if r["s4_outbox_ok"] else " ⚠ outbox≠rows"
        lines.append(
            f"| {k[4].upper()} | {k[0]} | {k[1]} | {k[2]} | {k[3]}{flag} | "
            f"{_fmt(r['target_rps'], 0)} | {_fmt(r['rps'], 0)}{behind} | "
            f"{_fmt(r['p50_ms'], 2)} | {_fmt(r['p90_ms'], 2)} | "
            f"{_fmt(r['p99_ms'], 2)} | {_fmt(r['p999_ms'], 2)} | {r['errors']} | "
            f"{_fmt(r['cpu_us_per_req'])} |"
        )
    lines.append("")
    return lines


def _crosscheck_table(agg: dict[Key, dict[str, Any]]) -> list[str]:
    checks = crosscheck(agg)
    if not checks:
        return []
    flagged = [c for c in checks if c["flag"]]
    tol = int(CROSSCHECK_TOLERANCE * 100)
    lines = [
        "### Load generator cross-check (oha vs wrk, same server instance)",
        "",
        (
            f"⚠ **{len(flagged)} of {len(checks)}** rows disagree by more than {tol}% "
            "— the client, not the server, may be the bottleneck there (or the "
            "measurement is noisy): treat those rps with care."
            if flagged
            else f"All {len(checks)} rows agree within {tol}%."
        ),
        "",
        "| python | workers | server | impl | scenario | oha rps | wrk rps | wrk vs oha |",
        "|---|---|---|---|---|---:|---:|---:|",
    ]
    for c in checks:
        py, w, server, impl, s = c["key"]
        mark = " ⚠" if c["flag"] else ""
        lines.append(
            f"| {py} | {w} | {server} | {impl} | {s.upper()} | {_fmt(c['oha'])} | "
            f"{_fmt(c['wrk'])} | {c['diff'] * 100:+.1f}%{mark} |"
        )
    lines.append("")
    return lines


def render(
    docs: list[dict[str, Any]], title: str = "Sandloom comparative benchmarks"
) -> str:
    agg = aggregate(docs)
    lines = [f"## {title}", ""]
    measured = [d for d in docs if d.get("results")]
    if measured:
        m = measured[0]["meta"]
        p = m.get("params", {})
        lines += [
            f"commit `{m.get('git_sha', '?')[:12]}` · load generators "
            f"`{m.get('load_generator')}` · {p.get('connections')} connections, "
            f"{p.get('client_threads', '?')} client threads · {p.get('duration_s')}s × "
            f"{p.get('reps')} reps (+{p.get('warmup_s')}s warm-up per scenario) · "
            "values are the MEDIAN of the reps",
            "",
            "> Compare implementations only **within one row group** (same job → same "
            "VM, interleaved reps). Rows from different jobs ran on different runner "
            "VMs. CPU µs/req = server process-tree (user+sys) CPU per successful "
            "request — the per-core efficiency metric, more stable than rps.",
            "",
        ]

    lines += [
        "### Runners",
        "",
        "| job | python | CPU | vCPUs | granian | uvicorn | load generators |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in docs:
        m = d.get("meta", {})
        v = m.get("versions", {})
        gens = m.get("load_generators") or {"oha": m.get("load_generator", "?")}
        lines.append(
            f"| {m.get('label', '?')} | {m.get('python', '?')} | {m.get('cpu_model', '?')} "
            f"| {m.get('cpu_count', '?')} | {v.get('granian', '?')} | {v.get('uvicorn', '?')} "
            f"| {'; '.join(f'{k}: {val}' for k, val in gens.items())} |"
        )
    lines.append("")

    lines += _headline(agg)
    lines += _crosscheck_table(agg)
    lines += _open_table(agg)
    lines += _closed_tables(agg)

    problems = []
    for d in docs:
        label = d.get("meta", {}).get("label", "?")
        if d.get("blocked"):
            problems.append(f"- **{label}** blocked: {d['blocked']}")
        for f in d.get("failures", []):
            err = " ".join(str(f.get("error", "")).split())[:400]
            problems.append(
                f"- **{label}** {f.get('impl')} on {f.get('server')} "
                f"(w{f.get('workers')}, rep {f.get('rep')}): {err}"
            )
    if problems:
        lines += ["### Failures / blocked", "", *problems, ""]
    return "\n".join(lines)


def best_servers(
    docs: list[dict[str, Any]], impl: str = "sandloom", top: int = 2
) -> list[str]:
    """Servers ranked by the geometric mean of ``impl``'s single-worker rps.

    Closed-loop rows only (the best generator per row). Only servers measured
    for every (python, scenario) present are ranked, so a server that failed a
    scenario never wins by omission.
    """
    agg = aggregate(docs)
    best: dict[tuple[Any, ...], float] = {}
    for k, v in agg.items():
        if k[1] == 1 and k[3] == impl and k[5] in CLOSED_LOOP and v["rps"]:
            best[k[:5]] = max(best.get(k[:5], 0.0), v["rps"])
    cells = {(k[0], k[4]) for k in best}
    per_server: dict[str, list[float]] = defaultdict(list)
    for k, rps in best.items():
        per_server[k[2]].append(rps)
    ranked = [
        (statistics.geometric_mean(vals), server)
        for server, vals in per_server.items()
        if len(vals) >= len(cells)
    ]
    ranked.sort(reverse=True)
    return [s for _, s in ranked[:top]]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("inputs", nargs="+", help="JSON files, CI logs, or - for stdin")
    p.add_argument("--json-out", type=Path, help="write the merged documents here")
    p.add_argument(
        "--best-servers",
        type=int,
        default=0,
        help="print the N best servers for Sandloom (comma list) and exit",
    )
    args = p.parse_args(argv)
    docs = load(args.inputs)
    if not docs:
        print("no benchmark documents found", file=sys.stderr)
        return 1
    if args.best_servers:
        print(",".join(best_servers(docs, top=args.best_servers)))
        return 0
    if args.json_out:
        args.json_out.write_text(json.dumps(docs, indent=2))
    print(render(docs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
