"""``sandloom security`` — incident-response commands (leak / compromise runbook).

    sandloom security revoke-sessions (--all | --tenant ORG | --user ID ...)
        end every session of the targets: the kernel revokes their access
        tokens (any issuer), the issuer ends what it holds itself (refresh
        sessions); the output says kind by kind what was done
    sandloom security force-password-reset (--all | --tenant ORG | --user ID ...)
        refuse password sign-in until the reset flow, and end every session
        (only where the issuer has passwords: refused otherwise)
    sandloom security audit-export --since T [--until T] [--action-prefix P]
                      [--database NAME ...] --output FILE
        the audit log of a time window as JSONL (sanitized metadata, no
        ``data``), e.g. for the incident record or as the off-site copy
        ``sandloom backup reconcile-security --audit-export`` reads
    sandloom security grant-admin --email ADDRESS
        make a registered user with a verified address a platform
        administrator (an unverified one: ``sandloom admin create --take-over``;
        a new one: ``sandloom admin create``)

Every command is DRY-RUN by default (targets / rows counted, nothing changed);
``--apply`` acts and needs ``--reason``; in production also
``--confirm-production``. Each applied action is audited: per affected user by
the kernel (``core.security.tokens_revoked``) and by the issuer (identity:
``identity.user.sessions_revoked`` / ``identity.user.password_reset_required``,
``actor_type="operator"``), and the export itself
(``core.security.audit_exported``).

Who answers (:mod:`sandloom.core.cli.operator_owner`): the session commands ask
the claim map's ``issuer`` (``<issuer>.security.v1``: ``end_sessions``,
``require_password_reset`` for a reset, ``list_user_ids`` for ``--all``) and,
for ``--tenant``, the owner of ``tenant_ids`` (``<owner>.security.v1``:
``list_member_user_ids``). Each method is confirmed by describe before the
first side effect, a dry run included; a missing one refuses (SANDLOOM-B039 /
SANDLOOM-B040, exit status 1) and nothing is done. ``grant-admin`` asks two
owners, both confirmed first (a dry run too): the issuer finds the account by
address (``account_by_email``), the authority of ``roles`` grants the role
(``appoint_platform_admin``), one write after every check; they may be
different modules.

What a session revocation guarantees (B-R5, a platform guarantee):

* **Sandloom access tokens**: revoked by the kernel for every issuer, in the
  command's own unit of work: one audit row per user, the revocation mark
  written before commit (``require_immediate``) and the durable
  ``core.auth.tokens_revoked.v1`` record in the same transaction's outbox,
  which every service's revocation applier
  (``sandloom:auth@tokens_revoked@<service>``) applies to its own cache. No
  success is reported before that unit commits.
* **Sandloom refresh sessions**: the issuer's, ended by its ``end_sessions``
  before the kernel revokes the access tokens (so none is refreshed past
  the mark); mandatory where the issuer declares ``refresh_sessions=True``.
* **External identity-provider sessions**: not controlled, unless the issuer
  says it ended them; the output says so.
* **Revocation is not a block**: a new sign-in stays possible. To refuse it,
  block the account (the kernel's ``block_user``).

Batches are deduplicated on an operation key derived from ``--operation-id`` +
the batch: re-running an interrupted command with the same ``--operation-id``
replays finished batches (the issuer's commands replay their stored result,
the kernel's part is claimed once per batch) instead of repeating them.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sandloom.core.auth.revocation as _revocation
import sandloom.core.cli.admin as _admin
import sandloom.core.cli.operator_owner as _operator_owner
import sandloom.core.di as _di
import sandloom.core.migrations.migrations as _migrations
from sandloom.core.auth.marks import RevocationUnavailableError
from sandloom.core.cli._operator import (
    OperatorIntent,
    add_operator_arguments,
    operator_intent,
)
from sandloom.core.cli.operator_owner import OperatorOwner
from sandloom.core.composition import load_composition_core
from sandloom.core.module_api._errors import ContractCallError
from sandloom.core.rpc.errors import RemoteServiceUnavailable
from sandloom.core.unit_of_work import db_session
from sandloom.core.unit_of_work.effects import record_audit
from sandloom.core.unit_of_work.scope import _audit_store

#: Audit action of an applied export.
AUDIT_EXPORTED_ACTION = "core.security.audit_exported"
#: Audit action, per user, of the kernel's access-token revocation.
AUDIT_TOKENS_REVOKED_ACTION = "core.security.tokens_revoked"
#: Durable claim scope (``consumer_inbox``) of a batch's kernel revocation:
#: a re-run with the same operation id never revokes that batch again.
REVOCATION_CLAIM = "sandloom-security@tokens_revoked"

#: The kinds of session a revocation reports, in output order.
SESSION_KINDS = ("access_tokens", "refresh_sessions", "provider_sessions")

_RESET_COMMAND = "force-password-reset"
_COMMANDS = ("revoke-sessions", _RESET_COMMAND)


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


Pager = Callable[[uuid.UUID | None, int], Awaitable[Sequence[Any]]]


@dataclass(frozen=True)
class SessionPlan:
    """The confirmed contracts of one session command (nothing called yet).

    ``issuer``: the owner of the sessions; ``sessions``: its contract with
    ``end_sessions``; ``credentials``: with ``require_password_reset``
    (``force-password-reset`` only); ``pager``: the target enumeration of
    ``--all`` / ``--tenant``; ``refresh_declared``: the issuer's
    ``refresh_sessions`` declaration when it is loaded here (``None``: it
    runs elsewhere, its answer is the report).
    """

    command: str
    issuer: OperatorOwner
    sessions: Any
    credentials: Any = None
    pager: Pager | None = None
    pager_owner: OperatorOwner | None = None
    refresh_declared: bool | None = None


@dataclass(slots=True)
class BulkResult:
    command: str
    targets: str
    applied: bool
    operation_id: str
    issuer: str = ""
    batches: int = 0
    users: int = 0
    affected: int = 0
    ended: set[str] = field(default_factory=set)

    def session_kinds(self) -> dict[str, str]:
        """What happened to each kind of session (the report)."""
        if not self.applied:
            return {
                "access_tokens": "would_revoke",
                "refresh_sessions": "issuer_reports",
                "provider_sessions": "issuer_reports",
                "sign_in": "still_possible",
            }
        return {
            "access_tokens": "revoked",
            "refresh_sessions": (
                "ended" if "refresh_sessions" in self.ended else "not_applicable"
            ),
            "provider_sessions": (
                "ended" if "provider_sessions" in self.ended else "not_controlled"
            ),
            "sign_in": "still_possible",
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "targets": self.targets,
            "applied": self.applied,
            "operation_id": self.operation_id,
            "issuer": self.issuer,
            "batches": self.batches,
            "users": self.users,
            "affected": self.affected,
            "sessions": self.session_kinds(),
            "password_reset": (
                ("required" if self.applied else "would_require")
                if self.command == _RESET_COMMAND
                else None
            ),
        }

    def lines(self) -> list[str]:
        """The per-kind report, one line each (text output)."""
        kinds = self.session_kinds()
        who = self.issuer or "the issuer"
        if not self.applied:
            out = [
                "  access tokens: would be revoked by the kernel (every issuer)",
                "  refresh sessions, external IdP sessions: as "
                f"{who}'s end_sessions reports",
            ]
        else:
            out = [
                "  access tokens revoked: the kernel's mark, durable "
                "(core.auth.tokens_revoked.v1, applied by every service)",
                f"  refresh sessions ended by {who}"
                if kinds["refresh_sessions"] == "ended"
                else f"  refresh sessions not applicable: {who} holds none",
                f"  external IdP sessions ended by {who}"
                if kinds["provider_sessions"] == "ended"
                else "  external IdP sessions not controlled: end them at the IdP",
            ]
        if self.command == _RESET_COMMAND:
            verb = "refused" if self.applied else "would be refused"
            out.append(f"  password sign-in {verb} until a reset ({who})")
        out.append(
            "  new sign-in still possible: revocation is not a block (block the "
            "account, the kernel's block_user, to refuse it)"
        )
        return out


def _build_core() -> Any:
    return load_composition_core()


def _refresh_declared(core: Any, owner: OperatorOwner) -> bool | None:
    for descriptor in getattr(core, "module_descriptors", ()) or ():
        if getattr(descriptor, "name", None) == owner.path:
            return bool(getattr(descriptor, "refresh_sessions", True))
    return None


async def plan_session_command(
    core: Any, command: str, targets: Targets
) -> SessionPlan:
    """Confirm every capability ``command`` needs for ``targets``, or refuse
    (SANDLOOM-B039 / B040, exit 1) before anything is done, a dry run too."""
    credentials = None
    if command == _RESET_COMMAND:
        _, credentials = await _operator_owner.operator_contract(
            core, "credentials", ["require_password_reset"], command=command
        )
    issuer, sessions = await _operator_owner.operator_contract(
        core, "sessions", ["end_sessions"], command=command
    )
    pager: Pager | None = None
    pager_owner: OperatorOwner | None = None
    if targets.all_users:
        pager_owner, accounts = await _operator_owner.operator_contract(
            core, "accounts", ["list_user_ids"], command=command
        )

        def pager(after: uuid.UUID | None, limit: int) -> Awaitable[Sequence[Any]]:
            return accounts.list_user_ids(after, limit)  # type: ignore[no-any-return]

    elif targets.tenant is not None:
        pager_owner, members = await _operator_owner.operator_contract(
            core, "tenant_members", ["list_member_user_ids"], command=command
        )
        org = targets.tenant

        def pager(after: uuid.UUID | None, limit: int) -> Awaitable[Sequence[Any]]:
            return members.list_member_user_ids(org, after, limit)  # type: ignore[no-any-return]

    return SessionPlan(
        command=command,
        issuer=issuer,
        sessions=sessions,
        credentials=credentials,
        pager=pager,
        pager_owner=pager_owner,
        refresh_declared=_refresh_declared(core, issuer),
    )


def _forbidden(exc: BaseException) -> bool:
    """The provider's 403 of a caller its ``RPC_ALLOWED_CALLS`` does not list
    (a domain 403 of the method itself is the provider's word, not a grant)."""
    return (
        isinstance(exc, RemoteServiceUnavailable)
        and getattr(exc, "code", None) == "rpc_caller_forbidden"
    )


async def _call(
    core: Any,
    owner: OperatorOwner,
    method: str,
    call: Awaitable[Any],
    *,
    resume: str,
) -> Any:
    """``call``, with a 403 named as the missing grant and any other failure
    as a resumable stop."""
    try:
        return await call
    except (ContractCallError, RemoteServiceUnavailable) as exc:
        if _forbidden(exc):
            raise _operator_owner.forbidden_refusal(core, owner, method) from exc
        raise SystemExit(
            f"error: {owner.command}: {owner.contract}.{method} failed "
            f"({type(exc).__name__}: {getattr(exc, 'detail', exc)}); {resume}"
        ) from exc


async def iter_target_batches(
    core: Any, plan: SessionPlan, targets: Targets, *, batch: int
) -> AsyncIterator[list[uuid.UUID]]:
    """The target user ids in batches of at most ``batch``."""
    if targets.users:
        ids = list(dict.fromkeys(targets.users))
        for start in range(0, len(ids), batch):
            yield ids[start : start + batch]
        return
    if plan.pager is None or plan.pager_owner is None:
        raise ValueError("targets without users need a confirmed pager")
    method = "list_user_ids" if targets.all_users else "list_member_user_ids"
    after: uuid.UUID | None = None
    while True:
        raw = await _call(
            core,
            plan.pager_owner,
            method,
            plan.pager(after, batch),
            resume="re-run with the same --operation-id to resume",
        )
        page = [uuid.UUID(str(u)) for u in raw]
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


async def revoke_access_tokens(
    ids: Sequence[uuid.UUID], *, command: str, intent: OperatorIntent, key: str
) -> bool:
    """The kernel's part: revoke every access token of ``ids``, any issuer.

    One unit of work in this process's ``default`` database: a durable claim
    of ``key`` (a re-run replays: ``False``), one audit row per user, and per
    user the revocation mark written before commit (``require_immediate``:
    an unreachable cache refuses and rolls back) with the durable
    ``core.auth.tokens_revoked.v1`` record in the same outbox, which every
    service's revocation applier applies to its own cache.
    """
    async with db_session() as session:
        if not await _di.durable_inbox(REVOCATION_CLAIM).claim(session, key):
            return False
        for user_id in ids:
            record_audit(
                AUDIT_TOKENS_REVOKED_ACTION,
                actor_id=None,
                actor_type="operator",
                resource_type="user",
                resource_id=str(user_id),
                data={
                    "command": command,
                    "operator": intent.operator,
                    "reason": intent.reason,
                    "operation_key": key,
                },
                required=True,
            )
        # Last fallible step: each mark is written before commit.
        for user_id in ids:
            await _revocation.revoke_user_tokens_on_commit(
                user_id,
                reason=f"operator ({intent.operator}): {intent.reason}",
                require_immediate=True,
            )
    return True


Revoker = Callable[..., Awaitable[bool]]


def _answer_field(answer: Any, name: str) -> Any:
    if isinstance(answer, Mapping):
        return answer.get(name)
    return getattr(answer, name, None)


async def _apply_batch(
    core: Any,
    plan: SessionPlan,
    ids: list[uuid.UUID],
    intent: OperatorIntent,
    *,
    operation_id: str,
    result: BulkResult,
    revoke: Revoker,
) -> int:
    """Issuer first (its refresh sessions end before the mark), then the
    kernel's revocation; how many users the issuer affected."""
    command = plan.command
    key = batch_operation_key(command, operation_id, ids)
    resume = (
        f"batches before {result.batches} are done; re-run with --operation-id "
        f"{operation_id} to resume (finished batches replay)"
    )
    reset: int | None = None
    if plan.credentials is not None:
        reset = await _call(
            core,
            plan.issuer,
            "require_password_reset",
            plan.credentials.require_password_reset(
                ids, intent.reason, intent.operator, key
            ),
            resume=resume,
        )
    sessions_key = (
        key
        if plan.credentials is None
        else batch_operation_key(f"{command}:end_sessions", operation_id, ids)
    )
    answer = await _call(
        core,
        plan.issuer,
        "end_sessions",
        plan.sessions.end_sessions(ids, intent.reason, intent.operator, sessions_key),
        resume=resume,
    )
    sessions_affected = int(_answer_field(answer, "affected") or 0)
    # A reset reports whose password it now requires; a revocation, whose
    # sessions the issuer ended.
    affected = sessions_affected if reset is None else int(reset)
    ended = {str(k) for k in (_answer_field(answer, "ended") or ())}
    try:
        await revoke(ids, command=command, intent=intent, key=key)
    except RevocationUnavailableError as exc:
        raise SystemExit(
            f"error: {command}: batch {result.batches}: the access tokens of "
            f"{len(ids)} user(s) are NOT revoked: the revocation store "
            "(security cache) is unreachable, so the kernel's unit rolled back "
            f"({exc}); {resume}"
        ) from exc
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(
            f"error: {command}: batch {result.batches}: the access tokens of "
            f"{len(ids)} user(s) are NOT revoked: the kernel's unit failed "
            f"({type(exc).__name__}: {exc}); {resume}"
        ) from exc
    if plan.refresh_declared and sessions_affected and "refresh_sessions" not in ended:
        raise SystemExit(
            f"error: {command}: {plan.issuer.label()} declares "
            "refresh_sessions=True, but its end_sessions reported "
            f"{sorted(ended) or 'nothing'} ended: its refresh sessions may "
            "still mint tokens. The access tokens of the batch are revoked; "
            f"fix {plan.issuer.module}'s end_sessions and {resume}"
        )
    result.ended |= ended & set(SESSION_KINDS)
    return affected


async def run_bulk(
    core: Any,
    command: str,
    targets: Targets,
    intent: OperatorIntent,
    *,
    operation_id: str,
    batch: int = 500,
    progress: Callable[[str], None] | None = None,
    revoke: Revoker | None = None,
) -> BulkResult:
    """Dry-run (count) or apply ``command`` to ``targets`` batch by batch.

    Every capability is confirmed first (:func:`plan_session_command`), a
    dry run included; nothing is done when one is missing. ``revoke`` is
    the kernel's part (default :func:`revoke_access_tokens`).
    """
    if command not in _COMMANDS:
        raise ValueError(f"unknown session command {command!r}")
    plan = await plan_session_command(core, command, targets)
    result = BulkResult(
        command=command,
        targets=targets.describe(),
        applied=intent.apply,
        operation_id=operation_id,
        issuer=plan.issuer.module,
    )
    async for ids in iter_target_batches(core, plan, targets, batch=batch):
        result.batches += 1
        result.users += len(ids)
        if not intent.apply:
            continue
        affected = await _apply_batch(
            core,
            plan,
            ids,
            intent,
            operation_id=operation_id,
            result=result,
            revoke=revoke or revoke_access_tokens,
        )
        result.affected += affected
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
        return 0
    print(summary_line(result))
    for line in result.lines():
        print(line)
    return 0


def summary_line(result: BulkResult) -> str:
    """The first line of a session command's text report."""
    if result.applied:
        return (
            f"{result.command}: {result.affected} user(s) affected of "
            f"{result.users} targeted ({result.targets}) in {result.batches} "
            f"batch(es); issuer {result.issuer}; operation id {result.operation_id}"
        )
    return (
        f"dry-run {result.command}: {result.users} user(s) targeted "
        f"({result.targets}) in {result.batches} batch(es); issuer "
        f"{result.issuer} confirmed; re-run with --apply --reason ... "
        f"(--operation-id {result.operation_id} to resume)"
    )


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


async def grant_admin(core: Any, email: str, intent: OperatorIntent) -> str:
    """What happened, in one line; a missing user is the operator's next step.

    Two owners, both confirmed before anything is read, a dry run included:
    the issuer finds the account (``account_by_email``), the authority of
    ``roles`` grants the role (``appoint_platform_admin``). One write, on
    the roles owner, after every check: never half done.
    """
    owners = await _admin.admin_owners(
        core,
        "grant-admin",
        issuer=["account_by_email"],
        roles=["appoint_platform_admin"],
    )
    standing = await _admin.account_standing(core, owners, email, with_role=False)
    if standing is None:
        raise SystemExit(
            f"error: no user {email!r}: create the administrator with "
            f"uv run sandloom admin create --email {email}"
        )
    user_id = standing.user_id
    if not standing.email_verified:
        # Whoever registered the address first may not be its owner: their
        # password must not become an administrator's.
        raise SystemExit(
            f"error: {email} has not verified its address, so its password may "
            "belong to someone else: uv run sandloom admin create --email "
            f"{email} --take-over (makes it an administrator and ends that password)"
        )
    if not intent.apply:
        return (
            f"dry-run grant-admin: {email} ({user_id}) would become a platform "
            "administrator; re-run with --apply --reason ..."
        )
    # A fresh key per run: granting again after a revocation must act, not replay.
    try:
        exists = await owners.roles.call(
            core,
            "appoint_platform_admin",
            user_id,
            intent.reason,
            intent.operator,
            uuid.uuid4().hex,
        )
    except ContractCallError as exc:
        raise SystemExit(
            f"error: grant-admin: {owners.roles.owner.contract} refused: "
            f"{getattr(exc, 'detail', exc)}"
        ) from exc
    if exists is False:
        raise SystemExit(
            f"error: grant-admin: {owners.roles.owner.label()} does not know user "
            f"{user_id} ({email}); nothing was granted"
        )
    return f"grant-admin: {email} ({user_id}) is a platform administrator"


def _cmd_grant(args: argparse.Namespace) -> int:
    intent = operator_intent(args)
    print(asyncio.run(grant_admin(_build_core(), args.email, intent)))
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

    grant = sub.add_parser(
        "grant-admin",
        help="make a registered user with a verified address a platform administrator",
    )
    grant.add_argument("--email", required=True, help="the user's address")
    add_operator_arguments(grant)

    args = parser.parse_args(argv)
    if args.command == "audit-export":
        return _cmd_export(args)
    if args.command == "grant-admin":
        return _cmd_grant(args)
    if not 1 <= args.batch <= 1000:
        raise SystemExit("error: --batch must be between 1 and 1000")
    return _cmd_bulk(args)


__all__ = [
    "AUDIT_TOKENS_REVOKED_ACTION",
    "REVOCATION_CLAIM",
    "SESSION_KINDS",
    "BulkResult",
    "SessionPlan",
    "Targets",
    "batch_operation_key",
    "export_audit",
    "iter_target_batches",
    "main",
    "plan_session_command",
    "revoke_access_tokens",
    "run_bulk",
    "summary_line",
]
