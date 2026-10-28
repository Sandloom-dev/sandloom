"""The UoW session bootstrap lives in ONE place (review map D-02).

Opening a Unit of Work binds the session for the UoW machinery: the factory
that opened it, the app-owned event bus and the database's outbox/audit
stores (``session.info``). That bootstrap used to be copied into every
session-opening path (request ``get_db``, ``Database(name)``, the read-only
request session, ``db_session()``, the contract runtime) and the copies
drifted. Kernel code now opens UoWs through ``unit_of_work.open_uow`` and
binds sessions through ``unit_of_work._bind_uow_session``; this test keeps a
new copy from appearing.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "sandloom"
OWNER = SRC / "core" / "unit_of_work"
BOOTSTRAP = re.compile(r"_UOW_FACTORY_KEY\]\s*=|_EVENT_BUS_KEY\]\s*=|\b_stash_stores\(")


def _runtime_sources() -> list[Path]:
    return [
        path
        for path in SRC.rglob("*.py")
        if OWNER not in path.parents and "tests" not in path.relative_to(SRC).parts
    ]


def test_only_unit_of_work_binds_uow_sessions() -> None:
    offenders = [
        f"{path.relative_to(SRC.parent)}:{lineno}"
        for path in _runtime_sources()
        for lineno, line in enumerate(path.read_text().splitlines(), 1)
        if BOOTSTRAP.search(line)
    ]
    assert offenders == [], (
        "UoW session bootstrap copied outside the sandloom/core/unit_of_work package; open "
        "the UoW with open_uow(core, name) or bind it with _bind_uow_session: "
        + ", ".join(offenders)
    )
