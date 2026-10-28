"""``sandloom.core.testing``: binding a UoW scope in tests for required audits."""

from __future__ import annotations

import pytest

from sandloom.core.di import AuditUnavailableError, record_audit
from sandloom.core.testing import RecordingSession, bind_test_uow, recording_uow


def test_required_audit_without_uow_raises() -> None:
    with pytest.raises(AuditUnavailableError):
        record_audit("x.sensitive", required=True)


def test_recording_uow_captures_required_audits() -> None:
    with recording_uow() as uow:
        record_audit(
            "x.sensitive",
            actor_id="00000000-0000-0000-0000-000000000001",
            resource_type="thing",
            resource_id="1",
            data={"password": "hunter2", "ok": 1},
            required=True,
        )
    assert uow.audit_actions() == ["x.sensitive"]
    assert uow.audits[0]["data"]["ok"] == 1
    assert uow.audits[0]["data"]["password"] != "hunter2"  # still redacted
    # The scope is unbound again afterwards.
    with pytest.raises(AuditUnavailableError):
        record_audit("x.sensitive", required=True)


def test_bind_test_uow_targets_the_named_database() -> None:
    session = RecordingSession()
    with (
        recording_uow(),
        bind_test_uow(
            session,
            database="analytics",
            audit=type("_A", (), {"record": lambda self, s, **f: s.audits.append(f)})(),  # type: ignore[arg-type]
            outbox=object(),  # type: ignore[arg-type]
        ),
    ):
        record_audit("a", required=True, database="analytics")
    assert session.audit_actions() == ["a"]
