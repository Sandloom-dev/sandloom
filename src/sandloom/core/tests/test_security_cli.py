"""``sandloom security`` orchestration: targets, batching, dry-run, replay keys.

The contracts are faked here (identity's real ``identity.security.v1`` is
covered in ``sandloom_identity/tests/api/test_security_recovery_ops.py``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from sandloom.core.cli._operator import OperatorIntent
from sandloom.core.cli.security import (
    MEMBERSHIP_CONTRACT,
    SECURITY_CONTRACT,
    Targets,
    batch_operation_key,
    main,
    run_bulk,
)

USERS = sorted(uuid.uuid4() for _ in range(7))
DRY = OperatorIntent(apply=False, operator="al", reason="")
APPLY = OperatorIntent(apply=True, operator="al", reason="db leak SEC-1")


class _Security:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[uuid.UUID], str, str, str]] = []

    async def list_user_ids(self, after: uuid.UUID | None, limit: int) -> list[Any]:
        rest = [u for u in USERS if after is None or u > after]
        return rest[:limit]

    async def revoke_sessions(
        self, ids: list[Any], reason: str, op: str, key: str
    ) -> int:
        self.calls.append(("revoke_sessions", ids, reason, op, key))
        return len(ids)

    async def require_password_reset(
        self, ids: list[Any], reason: str, op: str, key: str
    ) -> int:
        self.calls.append(("require_password_reset", ids, reason, op, key))
        return len(ids) - 1


class _Membership:
    def __init__(self, org: uuid.UUID) -> None:
        self.org = org

    async def list_member_user_ids(
        self, org: uuid.UUID, after: uuid.UUID | None, limit: int
    ) -> list[Any]:
        assert org == self.org
        rest = [u for u in USERS[:3] if after is None or u > after]
        return rest[:limit]


class _Core:
    def __init__(self, **services: Any) -> None:
        self.services = services

    def get_service(self, name: str) -> Any:
        from sandloom.core.module_api._service_registry import ServiceNotFound

        if name not in self.services:
            raise ServiceNotFound(name)
        return self.services[name]


async def test_dry_run_counts_targets_and_calls_no_command() -> None:
    security = _Security()
    core = _Core(**{SECURITY_CONTRACT: security})
    result = await run_bulk(
        core,
        "revoke-sessions",
        Targets(all_users=True),
        DRY,
        operation_id="op",
        batch=3,
    )
    assert (result.users, result.batches, result.affected) == (7, 3, 0)
    assert security.calls == []


async def test_apply_all_users_in_keyed_batches() -> None:
    security = _Security()
    core = _Core(**{SECURITY_CONTRACT: security})
    result = await run_bulk(
        core,
        "force-password-reset",
        Targets(all_users=True),
        APPLY,
        operation_id="op",
        batch=3,
    )
    assert result.affected == 7 - 3
    assert [c[1] for c in security.calls] == [USERS[:3], USERS[3:6], USERS[6:]]
    assert {c[0] for c in security.calls} == {"require_password_reset"}
    assert all(c[2:4] == ("db leak SEC-1", "al") for c in security.calls)
    keys = [c[4] for c in security.calls]
    assert len(set(keys)) == 3
    # Re-running the same operation sends the same keys: finished batches replay.
    again = _Security()
    await run_bulk(
        _Core(**{SECURITY_CONTRACT: again}),
        "force-password-reset",
        Targets(all_users=True),
        APPLY,
        operation_id="op",
        batch=3,
    )
    assert [c[4] for c in again.calls] == keys


async def test_tenant_targets_page_through_membership() -> None:
    org = uuid.uuid4()
    security = _Security()
    core = _Core(**{SECURITY_CONTRACT: security, MEMBERSHIP_CONTRACT: _Membership(org)})
    result = await run_bulk(
        core, "revoke-sessions", Targets(tenant=org), APPLY, operation_id="t", batch=2
    )
    assert result.users == 3
    assert [c[1] for c in security.calls] == [USERS[:2], USERS[2:3]]


async def test_listed_users_are_deduplicated() -> None:
    security = _Security()
    core = _Core(**{SECURITY_CONTRACT: security})
    targets = Targets(users=(USERS[0], USERS[1], USERS[0]))
    result = await run_bulk(core, "revoke-sessions", targets, APPLY, operation_id="u")
    assert result.users == 2
    assert security.calls[0][1] == [USERS[0], USERS[1]]


async def test_missing_contract_is_an_explicit_error() -> None:
    with pytest.raises(SystemExit, match=SECURITY_CONTRACT):
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
