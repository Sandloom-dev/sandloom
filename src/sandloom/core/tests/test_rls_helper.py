"""``enable_tenant_rls`` / ``disable_tenant_rls`` with ``if_configured=True``: the
form a generated revision calls, one line that acts only where the chain runner
switched tenant row security on for the database (the INFRA ``tenant_rls``)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from sandloom.core.sql_alchemy.rls import (
    TENANT_RLS_SWITCH,
    disable_tenant_rls,
    enable_tenant_rls,
)


class _Op:
    """The Alembic op's surface: ``execute`` and the migration context, whose
    ``config.attributes`` is where the runner leaves the switch."""

    def __init__(self, switch: bool | None, *, config: bool = True) -> None:
        self.sql: list[str] = []
        attributes: dict[str, Any] = (
            {} if switch is None else {TENANT_RLS_SWITCH: switch}
        )
        self._context = SimpleNamespace(
            dialect=postgresql.dialect(),
            config=SimpleNamespace(attributes=attributes) if config else None,
        )

    def execute(self, sql: Any) -> None:
        self.sql.append(str(sql))

    def get_context(self) -> Any:
        return self._context


def test_if_configured_enables_where_the_runner_switched_rls_on() -> None:
    op = _Op(True)
    enable_tenant_rls(op, "notes_note", if_configured=True)
    joined = "\n".join(op.sql)
    assert 'ALTER TABLE "notes_note" ENABLE ROW LEVEL SECURITY' in joined
    assert 'ALTER TABLE "notes_note" FORCE ROW LEVEL SECURITY' in joined
    down = _Op(True)
    disable_tenant_rls(down, "notes_note", if_configured=True)
    assert any("DISABLE ROW LEVEL SECURITY" in s for s in down.sql)


@pytest.mark.parametrize(
    "op",
    [_Op(False), _Op(None)],
    ids=["switch off", "switch absent"],
)
def test_if_configured_does_nothing_where_the_switch_is_not_on(op: _Op) -> None:
    enable_tenant_rls(op, "notes_note", if_configured=True)
    enable_tenant_rls(op, "notes_note", system_access=True, if_configured=True)
    disable_tenant_rls(op, "notes_note", if_configured=True)
    assert op.sql == []


def test_if_configured_refuses_a_context_with_no_alembic_config() -> None:
    """A bare ``Operations(MigrationContext.configure(conn))`` has no config to
    carry the switch: a silent "off" would leave the table without its policy."""
    for call in (enable_tenant_rls, disable_tenant_rls):
        op = _Op(None, config=False)
        with pytest.raises(RuntimeError, match="has none"):
            call(op, "notes_note", if_configured=True)
        assert op.sql == []
    # Without if_configured the config is not needed: the helper acts as before.
    op = _Op(None, config=False)
    enable_tenant_rls(op, "notes_note")
    assert any("ENABLE ROW LEVEL SECURITY" in s for s in op.sql)


def test_the_default_acts_whatever_the_switch() -> None:
    """Without ``if_configured`` the helper is what it was: it always acts (and
    refuses a database without row security), the runner's switch is not read."""
    op = _Op(False)
    enable_tenant_rls(op, "notes_note")
    disable_tenant_rls(op, "notes_note")
    joined = "\n".join(op.sql)
    assert (
        "ENABLE ROW LEVEL SECURITY" in joined and "DISABLE ROW LEVEL SECURITY" in joined
    )


def test_if_configured_keeps_the_arguments_checks() -> None:
    """The switch is read first, but a switched-on database still validates the
    table name, and ``system_access`` still reaches the policy."""
    with pytest.raises(ValueError, match="identifier"):
        enable_tenant_rls(_Op(True), 'x"; DROP TABLE users; --', if_configured=True)
    op = _Op(True)
    enable_tenant_rls(op, "notes_note", system_access=True, if_configured=True)
    assert 'TO "sandloom_system" USING (true) WITH CHECK (true)' in "\n".join(op.sql)
