"""``sandloom routes`` — per-module route snapshots (method, path, auth, policy).

    sandloom routes snapshot                 print every loaded module's snapshot
    sandloom routes snapshot --write         (re)write <module>/routes.snapshot.json
    sandloom routes snapshot --check         exit 1 if a committed snapshot is stale
    sandloom routes snapshot --module notes  limit to module(s)
    sandloom routes reference [--write PATH] the generated route reference page

A module's ``routes.snapshot.json`` is the approval of its routes' exposure
and derived policy (ADR-0009, model E): regenerate it after adding or
changing a route and review the diff (SANDLOOM-T001). ``sandloom test`` refreshes it in
dev but never over a WEAKER policy (spec §5.8: authentication, a risk,
idempotency, the session check, audit, step-up or an authorization removed);
``--write`` is the explicit approval of one and prints each it approves.
Routes the kernel mounts (ops endpoints, the platform console) are decided by
the kernel and are not in any module snapshot.

Builds the deployment's application (``SANDLOOM_COMPOSITION_ROOT``, default
``sandloom.main``); no startup task, consumer or relay runs. Exit codes: 0 current,
1 stale, 2 usage or I/O error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import sandloom.core.composition as _composition
from sandloom.core import route_snapshots as rs
from sandloom.core.cli.route_weakening import weakenings as _weakenings
from sandloom.core.descriptors.module_names import module_name as _module_name


def _build_app() -> Any:
    return _composition.load_composition_app()


def _select(docs: dict[str, Any], only: list[str]) -> dict[str, Any]:
    if not only:
        return docs
    return {m: d for m, d in docs.items() if m in only or _module_name(m) in only}


def _cmd_snapshot(write: bool, check: bool, only: list[str]) -> int:
    app = _build_app()
    # --write attaches what the modules' tests observed (informational)
    docs = _select(rs.module_route_snapshots(app, observed=write), only)
    if not write and not check:
        print(json.dumps(docs, indent=2, sort_keys=True))
        return 0
    if check:
        problems = rs.snapshot_problems(app, list(docs))
        if problems:
            print(
                "route snapshots out of date:\n  " + "\n  ".join(problems),
                file=sys.stderr,
            )
            return 1
        print(f"route snapshots up to date ({len(docs)} module(s))")
        return 0
    for module, doc in sorted(docs.items()):
        path = rs.snapshot_path(module)
        if path is None:
            print(
                f"error: cannot locate package directory of {module}", file=sys.stderr
            )
            return 2
        text = rs.render_snapshot(doc)
        for line in weakenings(path, doc):
            print(f"approved weaker route policy: {module}: {line}")
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path}")
    return 0


def weakenings(path: Path, doc: dict[str, Any]) -> list[str]:
    """How ``doc`` weakens the committed snapshot at ``path`` (G-ROUTE-04):
    ``sandloom test`` refuses these; ``--write`` approves them, saying so."""
    current = path.read_text(encoding="utf-8") if path.is_file() else None
    return _weakenings(current, doc)


def _cmd_reference(target: Path | None) -> int:
    text = rs.render_reference(_build_app())
    if target is None:
        sys.stdout.write(text)
        return 0
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom routes", description="Per-module route snapshots"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    snap = sub.add_parser("snapshot", help="print / write / verify module snapshots")
    mode = snap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write the snapshot files")
    mode.add_argument(
        "--check", action="store_true", help="exit 1 if a snapshot file is stale"
    )
    snap.add_argument(
        "--module", action="append", default=[], help="limit to module(s)"
    )
    ref = sub.add_parser("reference", help="print the generated route reference")
    ref.add_argument("--write", type=Path, default=None, help="write it to this file")
    args = parser.parse_args(argv)
    if args.action == "snapshot":
        return _cmd_snapshot(args.write, args.check, list(args.module))
    return _cmd_reference(args.write)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
