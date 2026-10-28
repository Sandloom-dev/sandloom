"""The planner's ORM-policy probe sees listeners added after it was built.

``policy_reason`` reads the ``Session`` class listeners through one probe
``Session`` built once per process (not one per planned read). That is sound
only because SQLAlchemy applies class-level listeners to EXISTING instances
too; these tests pin it, so a SQLAlchemy upgrade that changed it fails here
instead of letting a newly installed row filter be skipped by the optimized
executor.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session

import sandloom.core.tenant  # noqa: F401 - installs the kernel's tenant guard
from sandloom.core.sql_alchemy import planner_safety


@pytest.fixture
def engine() -> Any:
    # Never connects: policy_reason reads listeners and options only.
    return create_async_engine("postgresql+asyncpg://u:p@localhost:1/none")


@pytest.mark.parametrize(
    ("event_name", "reason"),
    [
        ("do_orm_execute", "custom ORM execute listener"),
        ("after_begin", "custom ORM session listener"),
        ("loaded_as_persistent", "custom ORM session listener"),
    ],
)
def test_a_listener_added_after_the_probe_disables_the_optimized_path(
    engine: Any, event_name: str, reason: str
) -> None:
    probe = planner_safety._session_probe()
    assert planner_safety.policy_reason(engine, []) is None

    def policy(*_args: Any) -> None:
        pass

    event.listen(Session, event_name, policy)
    try:
        assert planner_safety._session_probe() is probe  # built once ...
        found = planner_safety.policy_reason(engine, [])
        assert found is not None and found.startswith(reason)  # ... sees it
    finally:
        event.remove(Session, event_name, policy)
    assert planner_safety.policy_reason(engine, []) is None


def test_the_probe_is_built_once_per_process(
    monkeypatch: pytest.MonkeyPatch, engine: Any
) -> None:
    monkeypatch.setattr(planner_safety, "_PROBE", None)
    built: list[Any] = []
    real = planner_safety.Session

    def counting_session(*args: Any, **kwargs: Any) -> Session:
        session = real(*args, **kwargs)
        built.append(session)
        return session

    monkeypatch.setattr(planner_safety, "Session", counting_session)
    for _ in range(3):
        assert planner_safety.policy_reason(engine, []) is None
    assert len(built) == 1
