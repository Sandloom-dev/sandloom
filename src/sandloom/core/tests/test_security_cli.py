"""``sandloom security`` orchestration: targets, batching, dry-run, replay keys.

The contracts are faked here, behind the operator resolver as the standard
map names them (identity's real ``identity.security.v1`` is covered in
``sandloom_identity/tests/api/test_security_recovery_ops.py``; the third-party
issuer, other owners, split and refusals in ``test_security_operator.py``).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from sandloom.core.app_factory.claim_authorities import build_claim_authorities
from sandloom.core.cli._operator import OperatorIntent
from sandloom.core.cli.operator_owner import OperatorCapabilityUnavailable
from sandloom.core.cli.security import (
    Targets,
    batch_operation_key,
    main,
    run_bulk,
)

USERS = sorted(uuid.uuid4() for _ in range(7))
DRY = OperatorIntent(apply=False, operator="al", reason="")
APPLY = OperatorIntent(apply=True, operator="al", reason="db leak SEC-1")
IDENTITY = "sandloom_identity"
TENANCY = "sandloom_tenancy"


def _schema(obj: Any) -> SimpleNamespace:
    names = {n for n in dir(type(obj)) if not n.startswith("_")}
    return SimpleNamespace(methods=names - {"calls", "contract_schema", "org"})


class _Security:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[uuid.UUID], str, str, str]] = []
        self.contract_schema = _schema(self)

    async def list_user_ids(self, after: uuid.UUID | None, limit: int) -> list[Any]:
        rest = [u for u in USERS if after is None or u > after]
        return rest[:limit]

    async def end_sessions(
        self, ids: list[Any], reason: str, op: str, key: str
    ) -> dict[str, Any]:
        self.calls.append(("end_sessions", ids, reason, op, key))
        return {"affected": len(ids), "ended": ["access_tokens", "refresh_sessions"]}

    async def require_password_reset(
        self, ids: list[Any], reason: str, op: str, key: str
    ) -> int:
        self.calls.append(("require_password_reset", ids, reason, op, key))
        return len(ids) - 1


class _Membership:
    def __init__(self, org: uuid.UUID) -> None:
        self.org = org
        self.contract_schema = _schema(self)

    async def list_member_user_ids(
        self, org: uuid.UUID, after: uuid.UUID | None, limit: int
    ) -> list[Any]:
        assert org == self.org
        rest = [u for u in USERS[:3] if after is None or u > after]
        return rest[:limit]


class _Core:
    """A one-process composition as the operator resolver reads it."""

    def __init__(self, **contracts: tuple[str, Any]) -> None:
        self.contracts = contracts
        self.claim_authorities = build_claim_authorities(
            {"issuer": IDENTITY, "roles": IDENTITY, "tenant_ids": TENANCY}
        )
        self.module_descriptors = [
            SimpleNamespace(name=path, refresh_sessions=True)
            for path, _ in contracts.values()
        ]
        self.service_registry = SimpleNamespace(try_get=self.contracts.get)
        self.settings = SimpleNamespace(service_bindings={})

    def contract_owner(self, name: str) -> str:
        return self.contracts[name][0]

    def local_contract(self, name: str) -> Any:
        return self.contracts[name][1]


def _identity(security: _Security, **more: tuple[str, Any]) -> _Core:
    return _Core(**{"identity.security.v1": (IDENTITY, security)}, **more)


class _Revoked:
    """The kernel's part, recorded (its real unit: test_security_operator)."""

    def __init__(self) -> None:
        self.batches: list[tuple[list[uuid.UUID], str, str]] = []

    async def __call__(
        self, ids: Any, *, command: str, intent: OperatorIntent, key: str
    ) -> bool:
        self.batches.append((list(ids), command, key))
        return True


async def test_dry_run_counts_targets_and_calls_no_command() -> None:
    security = _Security()
    revoked = _Revoked()
    result = await run_bulk(
        _identity(security),
        "revoke-sessions",
        Targets(all_users=True),
        DRY,
        operation_id="op",
        batch=3,
        revoke=revoked,
    )
    assert (result.users, result.batches, result.affected) == (7, 3, 0)
    assert security.calls == []
    assert revoked.batches == []
    assert result.as_dict()["sessions"]["access_tokens"] == "would_revoke"


async def test_apply_all_users_in_keyed_batches() -> None:
    security = _Security()
    revoked = _Revoked()
    result = await run_bulk(
        _identity(security),
        "force-password-reset",
        Targets(all_users=True),
        APPLY,
        operation_id="op",
        batch=3,
        revoke=revoked,
    )
    assert result.affected == 7 - 3
    resets = [c for c in security.calls if c[0] == "require_password_reset"]
    assert [c[1] for c in resets] == [USERS[:3], USERS[3:6], USERS[6:]]
    assert all(c[2:4] == ("db leak SEC-1", "al") for c in security.calls)
    keys = [c[4] for c in resets]
    assert len(set(keys)) == 3
    # The kernel revokes each batch after the issuer, under the batch's key.
    assert [b[2] for b in revoked.batches] == keys
    assert result.as_dict()["password_reset"] == "required"
    # Re-running the same operation sends the same keys: finished batches replay.
    again = _Security()
    await run_bulk(
        _identity(again),
        "force-password-reset",
        Targets(all_users=True),
        APPLY,
        operation_id="op",
        batch=3,
        revoke=_Revoked(),
    )
    assert [c[4] for c in again.calls] == [c[4] for c in security.calls]


async def test_tenant_targets_page_through_the_tenant_ids_owner() -> None:
    org = uuid.uuid4()
    security = _Security()
    core = _identity(security, **{"tenancy.security.v1": (TENANCY, _Membership(org))})
    result = await run_bulk(
        core,
        "revoke-sessions",
        Targets(tenant=org),
        APPLY,
        operation_id="t",
        batch=2,
        revoke=_Revoked(),
    )
    assert result.users == 3
    assert [c[1] for c in security.calls] == [USERS[:2], USERS[2:3]]
    assert result.session_kinds()["refresh_sessions"] == "ended"


async def test_listed_users_are_deduplicated() -> None:
    security = _Security()
    targets = Targets(users=(USERS[0], USERS[1], USERS[0]))
    result = await run_bulk(
        _identity(security),
        "revoke-sessions",
        targets,
        APPLY,
        operation_id="u",
        revoke=_Revoked(),
    )
    assert result.users == 2
    assert security.calls[0][1] == [USERS[0], USERS[1]]


async def test_missing_contract_is_an_explicit_error() -> None:
    with pytest.raises(OperatorCapabilityUnavailable, match="identity.security.v1"):
        await run_bulk(
            _Core(), "revoke-sessions", Targets(all_users=True), DRY, operation_id="x"
        )


def test_batch_key_depends_on_operation_and_members() -> None:
    a = batch_operation_key("revoke-sessions", "op", USERS[:2])
    assert a == batch_operation_key("revoke-sessions", "op", USERS[:2])
    assert a != batch_operation_key("revoke-sessions", "op2", USERS[:2])
    assert a != batch_operation_key("revoke-sessions", "op", USERS[:3])
    assert a != batch_operation_key("force-password-reset", "op", USERS[:2])


def test_cli_requires_exactly_one_target_and_a_reason_to_apply() -> None:
    with pytest.raises(SystemExit, match="exactly one"):
        main(["revoke-sessions"])
    with pytest.raises(SystemExit, match="exactly one"):
        main(["revoke-sessions", "--all", "--user", str(USERS[0])])
    with pytest.raises(SystemExit, match="--reason"):
        main(["revoke-sessions", "--all", "--apply"])
