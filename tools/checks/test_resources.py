#!/usr/bin/env python3
"""List / prune the shared test databases and Redis indexes of this machine.

Every test run marks the PostgreSQL databases and Redis indexes it resets with
its owner (``sandloom.core.testing.ownership``) and stamps when it last used them.
Markers of long-finished runs pile up until a parallel run finds no free Redis
index for its workers. This tool shows them and removes the stale ones.

    uv run python tools/checks/test_resources.py list [--all]
    uv run python tools/checks/test_resources.py prune [--days 7] [--include-unmarked]
    uv run python tools/checks/test_resources.py prune --days 7 --apply

``prune`` is a DRY RUN unless ``--apply`` is given. A resource is pruned only
when nothing is connected to it and its last use is known and older than
``--days`` (minimum 1): a Redis index is emptied by an atomic check-and-FLUSHDB
(skipped if its marker / stamp changed since the scan), a database is dropped
with a plain DROP DATABASE (PostgreSQL refuses it while anything is
connected). The servers default to ``.env.test``'s DATABASE_URL / CACHE_URL
(the ``SANDLOOM_TEST_*`` overrides win); ``--redis`` / ``--database`` pick others.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND / "src"))

from sandloom.core.testing import resources  # noqa: E402
from sandloom.core.testing.ownership import current_owner  # noqa: E402


def _defaults() -> tuple[str, str]:
    from dotenv import dotenv_values

    values = dotenv_values(BACKEND / ".env.test")
    database = os.environ.get("SANDLOOM_TEST_DATABASE_URL") or values.get(
        "DATABASE_URL"
    )
    redis = os.environ.get("SANDLOOM_TEST_CACHE_URL") or values.get("CACHE_URL")
    return str(database or ""), str(redis or "")


def _size(resource: resources.Resource) -> str:
    if resource.kind == "redis":
        return f"{resource.size} keys"
    return f"{resource.size / 1_048_576:.0f} MB"


def _age(resource: resources.Resource, now: float) -> str:
    age = resource.age_days(now)
    return "?" if age is None else f"{age:.1f}d"


def _scan(args: argparse.Namespace) -> list[tuple[str, resources.Resource]]:
    found: list[tuple[str, resources.Resource]] = []
    if args.redis:
        found.extend((args.redis, r) for r in resources.scan_redis(args.redis))
    if args.database:
        databases = resources.scan_databases(args.database)
        found.extend((args.database, r) for r in databases)
    return found


def _print(rows: list[tuple[str, resources.Resource, str]], now: float) -> None:
    for verdict, r, why in rows:
        print(
            f"{verdict:6} {r.kind:8} {r.name:40} {(r.owner or '-')[:48]:48} "
            f"{_age(r, now):>7} {r.source:7} {r.live:>3} {_size(r):>10}  {why}"
        )


def main(argv: list[str] | None = None) -> int:
    database, redis = _defaults()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("list", "prune"))
    parser.add_argument("--redis", default=redis, help="any URL on the Redis server")
    parser.add_argument("--database", default=database, help="any URL on the server")
    parser.add_argument("--days", type=float, default=resources.DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--include-unmarked", action="store_true")
    parser.add_argument(
        "--protect", action="append", default=[], help="owner never pruned"
    )
    parser.add_argument("--apply", action="store_true", help="really prune")
    parser.add_argument("--all", action="store_true", help="list kept ones too")
    args = parser.parse_args(argv)

    now = time.time()
    protect = [*args.protect, current_owner()]
    rows = []
    for server, r in _scan(args):
        stale, why = resources.verdict(
            r,
            now=now,
            max_age_days=args.days,
            include_unmarked=args.include_unmarked,
            protect_owners=protect,
        )
        rows.append((server, r, stale, why))

    if args.command == "list":
        _print([("stale" if s else "keep", r, why) for _, r, s, why in rows], now)
        owners = Counter((r.kind, r.owner or "<unmarked>") for _, r, _, _ in rows)
        print(f"\n{len(rows)} resources; by owner:")
        for (kind, owner), n in sorted(owners.items(), key=lambda kv: -kv[1]):
            print(f"  {n:4} {kind:8} {owner}")
        return 0

    stale_rows = [(server, r, why) for server, r, s, why in rows if s]
    mode = "PRUNE" if args.apply else "would prune (dry run; --apply to act)"
    out = []
    for server, r, why in stale_rows:
        if args.apply:
            done = resources.prune(server, r)
            out.append(("pruned" if done else "skip", r, why if done else "changed"))
        else:
            out.append(("prune?", r, why))
    print(
        f"{mode}: {len(stale_rows)} of {len(rows)} resources (older than {args.days}d)"
    )
    _print(out, now)
    kept = Counter(why.split(" (")[0].split(" ")[0] for _, _, s, why in rows if not s)
    print(f"\nkept: {dict(kept)}")
    by_kind = Counter(r.kind for _, r, _ in stale_rows)
    print(f"stale by kind: {dict(by_kind)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
