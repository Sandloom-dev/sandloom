"""``sandloom backup`` — backup plan, restore verification, security-state repair.

    sandloom backup plan [--json]
        what to back up per database instance / Redis endpoint, and how
    sandloom backup verify <dump> [--database NAME] [--server-url URL]
                      [--pg-restore CMD] [--psql CMD] [--restore-url URL]
                      [--keep] [--json]
        restore the dump into a scratch database, check migration heads and
        integrity, drop it (exit 0 = the backup restores and is current)
    sandloom backup reconcile-security [--audit-export FILE ...] [--lift-stale-blocks]
                      [--apply [--confirm-production] --reason R] [--json]
        after restoring the SECURITY cache from a snapshot: re-derive block
        and revocation marks from the audit log / outbox (dry-run by default;
        exit 0 = nothing missing / everything re-applied, 1 = action needed)

The commands build the composition root (``SANDLOOM_COMPOSITION_ROOT``) to resolve
the configured databases and modules but do NOT start its lifespan. See
docs/operations/operator-reference.md → "Backups and restore" and
"Security incident runbook".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import sandloom.core.migrations.migrations as _migrations
from sandloom.core.auth.recovery import (
    read_block_events,
    read_block_events_file,
    read_revocations,
    reconcile_security_state,
    revocation_window_start,
)
from sandloom.core.cli._operator import add_operator_arguments, operator_intent
from sandloom.core.composition import load_composition_core
from sandloom.core.config.settings import settings
from sandloom.core.migrations.backup import (
    build_backup_plan,
    command_restorer,
    database_chains,
    format_plan,
    verify_dump,
)
from sandloom.core.unit_of_work import db_session
from sandloom.core.unit_of_work.effects import record_audit
from sandloom.core.unit_of_work.scope import _audit_store, _outbox_store


def _build_core() -> Any:
    return load_composition_core()


def _descriptors(core: Any) -> list[Any]:
    return list(getattr(core, "module_descriptors", None) or [])


def _cmd_plan(args: argparse.Namespace) -> int:
    items = build_backup_plan(settings, _descriptors(_build_core()))
    if args.json:
        print(json.dumps([i.as_dict() for i in items], indent=2))
    else:
        print(format_plan(items), end="")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    if settings.is_prod and not args.server_url and not args.confirm_production:
        print(
            "error: in production pass --server-url of a SCRATCH server (the "
            "restore creates and drops a database there), or --confirm-production "
            "to use the instance's own server",
            file=sys.stderr,
        )
        return 2
    core = _build_core()
    server = args.server_url or _migrations.resolve_database_sync_url(args.database)
    report = verify_dump(
        args.dump,
        server_url=server,
        chains=database_chains(args.database, _descriptors(core)),
        restorer=command_restorer(
            pg_restore=args.pg_restore, psql=args.psql, restore_url=args.restore_url
        ),
        keep=args.keep,
    )
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        for check in report.checks:
            mark = "ok  " if check.ok else "FAIL"
            print(f"{mark} {check.name}: {check.detail}")
        verdict = "OK" if report.ok else "FAILED"
        kept = f" (kept: {report.scratch})" if report.kept else ""
        print(f"restore verification {verdict}{kept}")
    return 0 if report.ok else 1


async def reconcile(
    core: Any,
    *,
    apply: bool,
    lift_stale_blocks: bool = False,
    audit_exports: list[str] | None = None,
    databases: list[str] | None = None,
) -> Any:
    """Gather the durable records of every database and reconcile the cache."""
    since = revocation_window_start()
    events: list[Any] = []
    revocations: dict[str, float] = {}
    oldest = None
    notes: list[str] = []
    for name in databases or sorted(_migrations.list_database_instances()):
        async with db_session(name) as session:
            audit = _audit_store(session)
            if audit is None:
                notes.append(f"database {name!r} has no audit store")
            else:
                events += await read_block_events(session, audit, source=name)
            try:
                outbox = _outbox_store(session)
            except Exception:  # noqa: BLE001 - driver without an outbox
                notes.append(f"database {name!r} has no outbox")
                continue
            marks, first = await read_revocations(session, outbox, since=since)
            for user, mark in marks.items():
                revocations[user] = max(mark, revocations.get(user, 0.0))
            if first is not None and (oldest is None or first < oldest):
                oldest = first
    for path in audit_exports or []:
        events += read_block_events_file(path)
    report = await reconcile_security_state(
        block_events=events,
        revocations=revocations,
        apply=apply,
        lift_stale_blocks=lift_stale_blocks,
        window_start=since,
        oldest_revocation_record=oldest,
    )
    report.warnings.extend(notes)
    return report


async def _audit_reconcile(report: Any, operator: str, reason: str) -> None:
    async with db_session("default"):
        record_audit(
            "core.security.marks_reconciled",
            actor_id=None,
            actor_type="operator",
            resource_type="security_cache",
            resource_id=None,
            data={
                "operator": operator,
                "reason": reason,
                "blocks_reapplied": len(report.missing_blocks),
                "revocations_reapplied": len(report.missing_revocations),
                "stale_blocks_lifted": len(report.lifted_stale_blocks),
                "failures": len(report.failures),
            },
            required=True,
        )


def _cmd_reconcile(args: argparse.Namespace) -> int:
    intent = operator_intent(args)
    core = _build_core()

    async def _run() -> Any:
        report = await reconcile(
            core,
            apply=intent.apply,
            lift_stale_blocks=args.lift_stale_blocks,
            audit_exports=args.audit_export,
            databases=args.database or None,
        )
        if intent.apply:
            await _audit_reconcile(report, intent.operator, intent.reason)
        return report

    report = asyncio.run(_run())
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        mode = "applied" if report.applied else "dry-run"
        print(f"security-state reconciliation ({mode})")
        print(f"  blocked users (durable records): {report.blocked_users}")
        print(f"  block marks missing from the cache: {len(report.missing_blocks)}")
        for user in report.missing_blocks[:20]:
            print(f"    {user}")
        print(
            f"  stale block marks (newest record is an unblock): "
            f"{len(report.stale_blocks)} (lifted: {len(report.lifted_stale_blocks)})"
        )
        print(
            f"  revocations in the window: {report.revocations_seen}, "
            f"missing/older in the cache: {len(report.missing_revocations)}"
        )
        for line in report.failures:
            print(f"  FAILURE {line}")
        for line in report.warnings:
            print(f"  WARNING {line}")
        print("clean" if report.clean else "ACTION NEEDED")
    return 0 if report.clean else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandloom backup", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="what to back up, and how")
    plan.add_argument("--json", action="store_true")

    verify = sub.add_parser("verify", help="restore a dump into a scratch DB + check")
    verify.add_argument("dump", help="pg_dump output (custom, tar, directory, plain)")
    verify.add_argument("--database", default="default", help="instance it backs up")
    verify.add_argument(
        "--server-url", default=None, help="scratch server (default: the instance's)"
    )
    verify.add_argument("--pg-restore", default="pg_restore", help="pg_restore command")
    verify.add_argument("--psql", default="psql", help="psql command (plain dumps)")
    verify.add_argument(
        "--restore-url",
        default=None,
        help="the server as the restore TOOL reaches it (containerised client)",
    )
    verify.add_argument("--keep", action="store_true", help="keep the scratch DB")
    verify.add_argument("--confirm-production", action="store_true")
    verify.add_argument("--json", action="store_true")

    rec = sub.add_parser(
        "reconcile-security", help="re-derive block / revocation marks after a restore"
    )
    rec.add_argument(
        "--audit-export",
        action="append",
        default=[],
        help="an `sandloom security audit-export` JSONL file (repeatable): blocks the "
        "restored database no longer records",
    )
    rec.add_argument(
        "--database",
        action="append",
        default=[],
        help="limit to these instances (default: every configured database)",
    )
    rec.add_argument(
        "--lift-stale-blocks",
        action="store_true",
        help="also delete block marks whose newest record is an unblock",
    )
    rec.add_argument("--json", action="store_true")
    add_operator_arguments(rec)

    args = parser.parse_args(argv)
    if args.command == "plan":
        return _cmd_plan(args)
    if args.command == "verify":
        return _cmd_verify(args)
    return _cmd_reconcile(args)


__all__ = ["main", "reconcile"]
