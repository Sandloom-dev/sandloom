"""``sandloom security`` — incident-response commands (leak / compromise runbook).

    sandloom security revoke-sessions (--all | --tenant ORG | --user ID ...)
        end every session of the targets: refresh tokens revoked, access
        tokens rejected from the next request
    sandloom security force-password-reset (--all | --tenant ORG | --user ID ...)
        refuse password sign-in until the reset flow, and end every session
    sandloom security audit-export --since T [--until T] [--action-prefix P]
                      [--database NAME ...] --output FILE
        the audit log of a time window as JSONL (sanitized metadata, no
        ``data``), e.g. for the incident record or as the off-site copy
        ``sandloom backup reconcile-security --audit-export`` reads

Every command is DRY-RUN by default (targets / rows counted, nothing changed);
``--apply`` acts and needs ``--reason``; in production also
``--confirm-production``. Each applied action is audited: per affected user
by identity (``identity.user.sessions_revoked`` /
``identity.user.password_reset_required``, ``actor_type="operator"``) and the
export itself (``core.security.audit_exported``).

The session commands call the token issuer's ``identity.security.v1`` contract
(local when identity is in this process, else through ``SERVICE_BINDINGS``
with an ``RPC_ALLOWED_CALLS`` grant) in batches, each deduplicated on an
operation key derived from ``--operation-id`` + the batch: re-running an
interrupted command with the same ``--operation-id`` replays finished batches
instead of repeating them. ``--tenant`` pages the members through
``tenancy.membership.v1``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sandloom.core.migrations.migrations as _migrations
from sandloom.core.cli._operator import (
    OperatorIntent,
    add_operator_arguments,
    operator_intent,
)
from sandloom.core.composition import load_composition_core
from sandloom.core.module_api._service_registry import ServiceNotFound
from sandloom.core.unit_of_work import db_session
from sandloom.core.unit_of_work.effects import record_audit
from sandloom.core.unit_of_work.scope import _audit_store

#: The token issuer's operator contract (provided by ``sandloom_identity``).
SECURITY_CONTRACT = "identity.security.v1"
#: Membership read contract used to resolve ``--tenant`` (``sandloom_tenancy``).
MEMBERSHIP_CONTRACT = "tenancy.membership.v1"
#: Audit action of an applied export.
AUDIT_EXPORTED_ACTION = "core.security.audit_exported"

_COMMANDS = {
    "revoke-sessions": "revoke_sessions",
    "force-password-reset": "require_password_reset",
}


@dataclass(frozen=True, slots=True)
class Targets:
    all_users: bool = False
    tenant: uuid.UUID | None = None
    users: tuple[uuid.UUID, ...] = ()

    def describe(self) -> str:
        if self.all_users:
            return "every user"
        if self.tenant is not None:
            return f"members of organization {self.tenant}"
        return f"{len(self.users)} listed user(s)"


@dataclass(slots=True)
class BulkResult:
    command: str
    targets: str
    applied: bool
    operation_id: str
    batches: int = 0
    users: int = 0
    affected: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "targets": self.targets,
            "applied": self.applied,
            "operation_id": self.operation_id,
            "batches": self.batches,
            "users": self.users,
            "affected": self.affected,
        }


def _build_core() -> Any:
    return load_composition_core()


def _service(core: Any, name: str) -> Any:
    try:
        return core.get_service(name)
    except ServiceNotFound as exc:
        raise SystemExit(
            f"error: contract {name!r} is not available in this process — run the "
            "command where its module is loaded, or bind it in SERVICE_BINDINGS"
        ) from exc


async def iter_target_batches(
    core: Any, targets: Targets, *, batch: int
) -> AsyncIterator[list[uuid.UUID]]:
    """The target user ids in batches of at most ``batch``."""
    if targets.users:
        ids = list(dict.fromkeys(targets.users))
        for start in range(0, len(ids), batch):
            yield ids[start : start + batch]
        return
    after: uuid.UUID | None = None
    if targets.all_users:
        security = _service(core, SECURITY_CONTRACT)

        def _page(after: uuid.UUID | None) -> Awaitable[list[uuid.UUID]]:
            return security.list_user_ids(after, batch)  # type: ignore[no-any-return]

    else:
        membership = _service(core, MEMBERSHIP_CONTRACT)
        org = targets.tenant

        def _page(after: uuid.UUID | None) -> Awaitable[list[uuid.UUID]]:
            return membership.list_member_user_ids(org, after, batch)  # type: ignore[no-any-return]

    while True:
        page = [uuid.UUID(str(u)) for u in await _page(after)]
        if not page:
            return
        yield page
        if len(page) < batch:
            return
        after = page[-1]


def batch_operation_key(
    command: str, operation_id: str, ids: Sequence[uuid.UUID]
) -> str:
    """Idempotency key of one batch: same operation + same users -> replay."""
    digest = hashlib.sha256(",".join(str(u) for u in ids).encode()).hexdigest()[:24]
    return f"sandloom-security:{command}:{operation_id}:{digest}"


async def run_bulk(
    core: Any,
    command: str,
    targets: Targets,
    intent: OperatorIntent,
    *,
    operation_id: str,
    batch: int = 500,
    progress: Callable[[str], None] | None = None,
) -> BulkResult:
    """Dry-run (count) or apply ``command`` to ``targets`` batch by batch."""
    method = _COMMANDS[command]
    result = BulkResult(
        command=command,
        targets=targets.describe(),
        applied=intent.apply,
        operation_id=operation_id,
    )
    security = _service(core, SECURITY_CONTRACT) if intent.apply else None
    async for ids in iter_target_batches(core, targets, batch=batch):
        result.batches += 1
        result.users += len(ids)
        if security is None:
            continue
        affected = await getattr(security, method)(
            ids,
            intent.reason,
            intent.operator,
            batch_operation_key(command, operation_id, ids),
        )
        result.affected += int(affected)
        if progress is not None:
            progress(f"batch {result.batches}: {affected}/{len(ids)} user(s)")
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


async def export_audit(  # noqa: C901
    *,
    databases: Sequence[str],
    since: datetime,
    until: datetime | None,
    action_prefix: str | None,
    intent: OperatorIntent,
    output: Path | None,
    page: int = 500,
) -> dict[str, Any]:
    """Count (dry-run) or write the window's audit rows as JSONL, newest first.

    With ``intent.apply`` the export is itself audited in each exported
    database (``core.security.audit_exported``: window, row count, SHA-256 of
    the file, operator, reason) — committed after the file is complete.
    """
    if intent.apply and output is None:
        raise SystemExit("error: --apply requires --output FILE")
    counts: dict[str, int] = {}
    digest = hashlib.sha256()
    handle = output.open("w", encoding="utf-8") if intent.apply and output else None
    try:
        for name in databases:
            counts[name] = 0
            async with db_session(name) as session:
                store = _audit_store(session)
                if store is None:
                    raise SystemExit(f"error: database {name!r} has no audit store")
                before: tuple[datetime, Any] | None = None
                while True:
                    rows = await store.query_page(
                        session,
                        limit=page,
                        before=before,
                        since=since,
                        until=until,
                        action_prefix=action_prefix,
                    )
                    for row in rows:
                        counts[name] += 1
                        if handle is not None:
                            line = json.dumps(
                                {"database": name}
                                | {k: _jsonable(v) for k, v in row.items()},
                                sort_keys=True,
                            )
                            handle.write(line + "\n")
                            digest.update(line.encode() + b"\n")
                    if len(rows) < page:
                        break
                    before = (rows[-1]["occurred_at"], rows[-1]["id"])
    finally:
        if handle is not None:
            handle.close()
    summary = {
        "applied": intent.apply,
        "since": since.isoformat(),
        "until": until.isoformat() if until else None,
        "action_prefix": action_prefix,
        "rows": counts,
        "output": str(output) if output else None,
        "sha256": digest.hexdigest() if handle is not None else None,
    }
    if intent.apply:
        for name in databases:
            async with db_session(name):
                record_audit(
                    AUDIT_EXPORTED_ACTION,
                    actor_id=None,
                    actor_type="operator",
                    resource_type="audit_log",
                    resource_id=None,
                    data={
                        "operator": intent.operator,
                        "reason": intent.reason,
                        "since": summary["since"],
                        "until": summary["until"],
                        "action_prefix": action_prefix,
                        "rows": counts[name],
                        "sha256": summary["sha256"],
                    },
                    required=True,
                )
    return summary


def _parse_time(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _targets(args: argparse.Namespace) -> Targets:
    chosen = sum((bool(args.all), args.tenant is not None, bool(args.user)))
    if chosen != 1:
        raise SystemExit("error: choose exactly one of --all, --tenant, --user")
    return Targets(
        all_users=bool(args.all),
        tenant=uuid.UUID(args.tenant) if args.tenant else None,
        users=tuple(uuid.UUID(u) for u in args.user),
    )


def _cmd_bulk(args: argparse.Namespace) -> int:
    targets = _targets(args)
    intent = operator_intent(args)
    operation_id = args.operation_id or uuid.uuid4().hex
    core = _build_core()

    def _progress(line: str) -> None:
        print(line, file=sys.stderr)

    result = asyncio.run(
        run_bulk(
            core,
            args.command,
            targets,
            intent,
            operation_id=operation_id,
            batch=args.batch,
            progress=_progress,
        )
    )
    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    elif result.applied:
        print(
            f"{result.command}: {result.affected} user(s) affected of "
            f"{result.users} targeted ({result.targets}) in {result.batches} "
            f"batch(es); operation id {result.operation_id}"
        )
    else:
        print(
            f"dry-run {result.command}: {result.users} user(s) targeted "
            f"({result.targets}) in {result.batches} batch(es); re-run with "
            f"--apply --reason ... (--operation-id {result.operation_id} to resume)"
        )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    intent = operator_intent(args)
    _build_core()
    databases = args.database or sorted(_migrations.list_database_instances())
    summary = asyncio.run(
        export_audit(
            databases=databases,
            since=_parse_time(args.since),
            until=_parse_time(args.until) if args.until else None,
            action_prefix=args.action_prefix,
            intent=intent,
            output=Path(args.output) if args.output else None,
        )
    )
    print(json.dumps(summary, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandloom security", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("revoke-sessions", "end every session of the targets"),
        ("force-password-reset", "require a password reset + end sessions"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--all", action="store_true", help="every user")
        p.add_argument("--tenant", default=None, help="members of this organization")
        p.add_argument("--user", action="append", default=[], help="a user id")
        p.add_argument("--batch", type=int, default=500, help="users per command")
        p.add_argument(
            "--operation-id", default=None, help="resume an interrupted run (replay)"
        )
        p.add_argument("--json", action="store_true")
        add_operator_arguments(p)

    exp = sub.add_parser("audit-export", help="export an audit-log time window")
    exp.add_argument(
        "--since", required=True, help="ISO 8601 (inclusive; UTC if naive)"
    )
    exp.add_argument("--until", default=None, help="ISO 8601 (exclusive)")
    exp.add_argument("--action-prefix", default=None, help="e.g. auth. or identity.")
    exp.add_argument(
        "--database", action="append", default=[], help="instance (default: all)"
    )
    exp.add_argument("--output", default=None, help="JSONL file to write")
    add_operator_arguments(exp)

    args = parser.parse_args(argv)
    if args.command == "audit-export":
        return _cmd_export(args)
    if not 1 <= args.batch <= 1000:
        raise SystemExit("error: --batch must be between 1 and 1000")
    return _cmd_bulk(args)


__all__ = [
    "SECURITY_CONTRACT",
    "BulkResult",
    "Targets",
    "batch_operation_key",
    "export_audit",
    "iter_target_batches",
    "main",
    "run_bulk",
]
