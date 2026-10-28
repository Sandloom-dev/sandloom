"""``sandloom sagas`` — list, inspect and operate saga instances (ADR-0008 §8, §14).

::

    sandloom sagas list [--type T] [--status S] [--version V] [--older-than 2h]
                   [--tenant ORG] [--all] [--database DB] [--json]
    sandloom sagas show <saga_type> <saga_id> [--json]
    sandloom sagas retry <saga_type> <saga_id>  --apply --reason R
    sandloom sagas park <saga_type> <saga_id>   --apply --reason R
    sandloom sagas resolve <saga_type> <saga_id> --step S --as applied|skipped \\
                   --apply --reason R

``list`` defaults to the **actionable** view — parked, compensating, and
running instances past their deadline — with their ages; ``--all`` shows
everything. ``show`` prints a timeline: one line per step call with its
outcome, attempts, duration and operation key, then why an instance is
parked and the fixed remediation for that reason.

The mutating commands are dry runs unless ``--apply --reason``; in
production they also need ``--confirm-production``. Every mutation is
audited (``core.saga.retried`` / ``.parked`` / ``.step_resolved``) in the
same transaction. ``retry`` refuses (exit 1) while the current step's
outcome is unknown and its command-log record may have expired — establish
the outcome at the provider and ``resolve`` it instead.

Cross-tenant reads and writes go through the system channel when the
database runs with ``tenant_rls``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from datetime import datetime, timedelta
from typing import Any

from sandloom.core.cli._operator import add_operator_arguments, operator_intent
from sandloom.core.composition import load_composition_core
from sandloom.core.descriptors import Saga
from sandloom.core.sagas import ops, runtime_of
from sandloom.core.sagas.outcomes import remediation
from sandloom.core.sagas.runtime import SagaRuntime
from sandloom.core.sql_alchemy.saga_store import InstanceRow, ListFilter

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_SYMBOL = {
    "succeeded": "✓",
    "refused": "✗",
    "failed": "↺",
    "resolved_applied": "≡",
    "resolved_skipped": "≡",
}


def parse_duration(raw: str) -> timedelta:
    match = _DURATION.match(raw.strip())
    if match is None:
        raise SystemExit(f"error: duration {raw!r} must look like 30s, 15m, 2h or 3d")
    return timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})


def _age(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _clock(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value is not None else "-"


def _status(row: InstanceRow) -> str:
    return f"{row.status} ({row.park_reason})" if row.park_reason else row.status


def _definition(runtime: SagaRuntime, row: InstanceRow) -> Saga[Any] | None:
    reg = runtime.registry.get(row.saga_type, row.definition_version)
    return reg.definition if reg is not None else None


def _current_step(runtime: SagaRuntime, row: InstanceRow) -> str:
    step, direction = ops.current_call(_definition(runtime, row), row)
    return f"{step}:{direction}" if step else "-"


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
def render_list(runtime: SagaRuntime, rows: list[InstanceRow], now: datetime) -> str:
    if not rows:
        return "no matching saga instances"
    header = f"{'saga_type':<28} {'saga_id':<38} {'status':<36} {'step':<24} age"
    lines = [header]
    lines.extend(
        f"{row.saga_type:<28} {row.saga_id:<38} {_status(row):<36} "
        f"{_current_step(runtime, row):<24} {_age(now - row.created_at)}"
        for row in rows
    )
    return "\n".join(lines)


def _filter(args: argparse.Namespace, now: datetime) -> ListFilter:
    older = parse_duration(args.older_than) if args.older_than else None
    return ListFilter(
        saga_type=args.type,
        status=tuple(args.status or ()),
        version=args.version,
        tenant_id=uuid.UUID(args.tenant) if args.tenant else None,
        created_before=now - older if older is not None else None,
        actionable_at=None if (args.all or args.status) else now,
        limit=args.limit,
    )


async def _list(runtime: SagaRuntime, args: argparse.Namespace) -> list[InstanceRow]:
    now = runtime.now()
    flt = _filter(args, now)
    databases = [args.database] if args.database else runtime.registry.databases()
    rows: list[InstanceRow] = []
    for database in databases:
        rows += await ops.list_instances(
            runtime, database, flt, operator=args.operator or ""
        )
    return sorted(rows, key=lambda r: r.created_at)


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #
def _step_line(step: dict[str, Any]) -> str:
    symbol = _SYMBOL.get(str(step["status"]), "·")
    first, last = step.get("first_attempt_at"), step.get("last_attempt_at")
    when = _clock(first)
    if first is not None and last is not None and last != first:
        when = f"{_clock(first)} → {_clock(last)}"
    attempts = int(step.get("attempts") or 0)
    parts = [f"  {symbol} {step['step']:<22} {step['direction']:<5} {when}"]
    if str(step["status"]).startswith("resolved_"):
        parts.append(
            f"resolved {str(step['status']).removeprefix('resolved_')} by an operator"
        )
    elif step["status"] == "succeeded":
        parts.append(f"{attempts} attempt{'s' if attempts != 1 else ''}")
        if step.get("last_duration_ms") is not None:
            parts.append(f"{float(step['last_duration_ms']):.0f} ms")
    else:
        error = step.get("last_error") or {}
        status = error.get("status") or ""
        code = error.get("code") or error.get("class") or ""
        parts.append(
            f"{attempts} attempts  last: {step['outcome']} {status} {code}".rstrip()
        )
    if step.get("operation_key"):
        parts.append(f"key {step['operation_key']}")
    return "  ".join(parts)


def render_show(row: InstanceRow, steps: list[dict[str, Any]]) -> str:
    tenant = str(row.tenant_id) if row.tenant_id else "-"
    lines = [
        f"{row.saga_type}  {row.saga_id}  status={_status(row)}  "
        f"v{row.definition_version}  tenant={tenant}  version={row.version}",
        f"  started {_clock(row.created_at)} by {row.started_by or '-'}  "
        f"trace={row.trace_id or '-'}  deadline {_clock(row.deadline_at)}",
    ]
    lines += [_step_line(s) for s in steps]
    if row.status == "parked":
        lines.append(f"  why parked: {row.park_reason}")
        lines.append(f"  next:       {remediation(row.park_reason)}")
        lines.append(
            f"              e.g. sandloom sagas retry {row.saga_type} {row.saga_id} "
            "--apply --reason ..."
        )
    elif row.status in ("running", "compensating"):
        lines.append(f"  next attempt {_clock(row.next_attempt_at)}")
    return "\n".join(lines)


def _json_row(row: InstanceRow) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(json.dumps(row.as_values(), default=str))
    return decoded


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def _intent(args: argparse.Namespace) -> ops.OperatorIntent:
    intent = operator_intent(args)
    return ops.OperatorIntent(
        apply=intent.apply, operator=intent.operator, reason=intent.reason
    )


def _print_result(result: ops.OperationResult) -> None:
    head = "APPLIED" if result.applied else "DRY-RUN (pass --apply --reason to act)"
    print(f"{head}: {result.action} {result.saga_type} {result.saga_id}")
    print(f"  {result.before} -> {result.after}: {result.message}")
    for hint in result.hints:
        print(f"  {hint}")


async def _dispatch(runtime: SagaRuntime, args: argparse.Namespace) -> int:
    if args.command == "list":
        rows = await _list(runtime, args)
        if args.json:
            print(json.dumps([_json_row(r) for r in rows], indent=2))
        else:
            print(render_list(runtime, rows, runtime.now()))
        return 0
    if args.command == "show":
        row, steps = await ops.show_instance(
            runtime, args.saga_type, args.saga_id, operator=args.operator or ""
        )
        if args.json:
            print(
                json.dumps(
                    {"instance": _json_row(row), "steps": steps}, default=str, indent=2
                )
            )
        else:
            print(render_show(row, steps))
        return 0
    intent = _intent(args)
    if args.command == "retry":
        result = await ops.retry(runtime, args.saga_type, args.saga_id, intent)
    elif args.command == "park":
        result = await ops.park(runtime, args.saga_type, args.saga_id, intent)
    else:
        result = await ops.resolve(
            runtime, args.saga_type, args.saga_id, args.step, args.as_, intent
        )
    _print_result(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandloom sagas", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    lst = sub.add_parser("list", help="list instances (default: the actionable view)")
    lst.add_argument("--type", default=None, help="saga type, e.g. enrollment.enroll")
    lst.add_argument("--status", action="append", default=[], help="repeatable")
    lst.add_argument("--version", type=int, default=None, help="definition version")
    lst.add_argument("--older-than", default=None, help="e.g. 30m, 2h, 3d")
    lst.add_argument("--tenant", default=None, help="tenant id")
    lst.add_argument(
        "--all", action="store_true", help="every instance, not only actionable"
    )
    lst.add_argument("--database", default=None, help="one database instance")
    lst.add_argument("--limit", type=int, default=100)
    lst.add_argument("--json", action="store_true")
    lst.add_argument("--operator", default=None, help=argparse.SUPPRESS)
    show = sub.add_parser("show", help="the timeline of one instance")
    show.add_argument("saga_type")
    show.add_argument("saga_id")
    show.add_argument("--json", action="store_true")
    show.add_argument("--operator", default=None, help=argparse.SUPPRESS)
    for name, text in (
        ("retry", "re-drive now (un-park, fresh deadline; post-TTL rule applies)"),
        ("park", "stop an active instance"),
        ("resolve", "assert the current step's outcome, then continue"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("saga_type")
        cmd.add_argument("saga_id")
        if name == "resolve":
            cmd.add_argument("--step", required=True, help="the current step")
            cmd.add_argument(
                "--as", dest="as_", required=True, choices=("applied", "skipped")
            )
        add_operator_arguments(cmd)
    return parser


async def run_command(argv: list[str], runtime: SagaRuntime) -> int:
    """Parse and run one command against ``runtime`` (exit code; tests)."""
    args = build_parser().parse_args(argv)
    try:
        return await _dispatch(runtime, args)
    except ops.SagaOperationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    build_parser().parse_args(args)  # usage errors before the app is built
    return asyncio.run(run_command(args, runtime_of(load_composition_core())))


__all__ = [
    "build_parser",
    "main",
    "parse_duration",
    "render_list",
    "render_show",
    "run_command",
]
