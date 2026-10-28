"""RBAC administration is audited, and narrowing a role ends its holders' sessions.

Role / permission CRUD changes who may do what: every mutation records a
``required=True`` audit row in the same UoW. Permissions are baked into
access-token claims, so revoking a permission from a role must revoke the
outstanding tokens of every user holding that role (like revoking the role
itself does).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom_identity.events import BaseEvent, EventActor
from sandloom_identity.models import UserRole
from sandloom_identity.schemas.rbac import PermissionCreate, RoleCreate, RoleUpdate
from sandloom_identity.services import role_service as role_mod
from sandloom_identity.services.role_service import PermissionService, RoleService


def capture_effects(monkeypatch: pytest.MonkeyPatch, module: Any) -> dict[str, Any]:
    """The audits ``module`` records and the identity events it publishes
    (``{"events": [(channel, payload)], "audit": [(action, kwargs)]}``),
    neither reaching a Unit of Work."""
    calls: dict[str, Any] = {"events": [], "audit": []}

    async def fake_publish(event: BaseEvent, *, database: str | None = None) -> None:
        calls["events"].append((event.channel, event.model_dump(mode="json")))

    def fake_audit(action: str, **kwargs: Any) -> None:
        calls["audit"].append((action, kwargs))

    monkeypatch.setattr(BaseEvent, "publish", fake_publish)
    monkeypatch.setattr(module, "record_audit", fake_audit)
    return calls


def _audits(calls: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return dict(calls["audit"])


@pytest.mark.asyncio
async def test_role_and_permission_crud_is_audited(
    test_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = capture_effects(monkeypatch, role_mod)
    actor = EventActor(user_id=uuid4())

    role = await RoleService.create_role(
        RoleCreate(name="Auditors", code="auditors"), test_db, actor
    )
    role_id = role.id
    await RoleService.update_role(
        role_id, RoleUpdate(description="reads audit logs"), test_db, actor
    )
    perm = await PermissionService.create_permission(
        PermissionCreate(
            name="Read audit", code="audit:read", resource="audit", action="read"
        ),
        test_db,
        actor,
    )
    perm_id = perm.id
    await PermissionService.assign_permission_to_role(role_id, perm_id, test_db, actor)
    await PermissionService.revoke_permission_from_role(
        role_id, perm_id, test_db, actor
    )
    await RoleService.delete_role(role_id, test_db, actor)

    audits = _audits(calls)
    for action in (
        "identity.role.created",
        "identity.role.updated",
        "identity.permission.created",
        "identity.role.permission_assigned",
        "identity.role.permission_revoked",
        "identity.role.deleted",
    ):
        assert action in audits, action
        assert audits[action]["required"] is True
        assert audits[action]["actor_id"] == actor.user_id
    assert audits["identity.role.permission_revoked"]["data"] == {
        "role_id": str(role_id),
        "role_code": "auditors",
        "permission_id": str(perm_id),
        "permission_code": "audit:read",
    }


@pytest.mark.asyncio
async def test_revoking_a_permission_revokes_holders_sessions(
    test_db: AsyncSession, make_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_effects(monkeypatch, role_mod)
    revoked: list[tuple[UUID, str]] = []

    async def fake_revoke_sessions(user_id: UUID, *, reason: str, **_: Any) -> None:
        revoked.append((user_id, reason))

    monkeypatch.setattr(role_mod, "revoke_sessions", fake_revoke_sessions)

    holder_a = await make_user("rbac-holder-a@example.com")
    holder_b = await make_user("rbac-holder-b@example.com")
    bystander = await make_user("rbac-bystander@example.com")
    role = await RoleService.create_role(
        RoleCreate(name="Exporters", code="exporters"), test_db
    )
    perm = await PermissionService.create_permission(
        PermissionCreate(
            name="Export", code="data:export", resource="data", action="export"
        ),
        test_db,
    )
    await PermissionService.assign_permission_to_role(role.id, perm.id, test_db)
    test_db.add_all(
        [
            UserRole(user_id=holder_a.id, role_id=role.id),
            UserRole(user_id=holder_b.id, role_id=role.id),
        ]
    )
    await test_db.flush()
    assert revoked == []  # granting never logs anyone out

    await PermissionService.revoke_permission_from_role(role.id, perm.id, test_db)

    assert sorted(revoked) == sorted(
        [
            (holder_a.id, "role_permission_revoked"),
            (holder_b.id, "role_permission_revoked"),
        ]
    )
    assert bystander.id not in {user_id for user_id, _ in revoked}
