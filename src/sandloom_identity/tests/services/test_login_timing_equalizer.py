"""Unknown and known emails queue on the argon2 pool exactly once per login,
and a failed login does the same externally timed work for both.

The password pool is bounded (``PASSWORD_HASH_CONCURRENCY``, 1-2 threads per
worker), so every submission waits its turn under a login burst. An unknown
email used to submit twice (fetch the dummy hash, then verify against it) and
so waited about twice as long as a known one — a timing side channel that
reveals whether an account exists, which the dummy verify is there to close.

The single submission is also made BEFORE the account lookup, so the place in
the pool's queue never depends on how long the lookup took (a hit is slower
than a miss, and under a burst that gap cost a known email a whole argon2
slot). The recorder tests below pin the rest: the same Redis commands, SQL
statements, pool submissions, audit writes and response for a wrong password
on a real account as for an unknown email — also on the attempt that locks.
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import HTTPException

from sandloom_identity.schemas.auth import LoginIn
from sandloom_identity.services import auth_service
from sandloom_identity.services.auth_service import AuthService

PW = "TestPassword123!"


@pytest.fixture
def pool_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    original = auth_service.run_password_hashing

    async def counting(fn: Callable[..., Any], *args: Any) -> Any:
        seen.append(getattr(fn, "__name__", repr(fn)))
        return await original(fn, *args)

    monkeypatch.setattr(auth_service, "run_password_hashing", counting)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [True, False], ids=["warm", "cold"])
async def test_unknown_email_submits_to_the_password_pool_once(
    test_db: Any,
    pool_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    warm: bool,
) -> None:
    monkeypatch.setattr(AuthService, "_DUMMY_HASH", None)
    if warm:
        await AuthService.warm_dummy_password_hash()
        assert AuthService._DUMMY_HASH is not None
        pool_calls.clear()

    with pytest.raises(HTTPException) as exc:
        await AuthService.login(
            LoginIn(email="nobody-here@example.com", password=PW), test_db
        )

    assert exc.value.status_code == 401
    assert pool_calls == ["_verify_login_password"]
    # Cold, the hash is made inside that same single submission.
    assert AuthService._DUMMY_HASH is not None


@pytest.mark.asyncio
async def test_known_email_submits_to_the_password_pool_once(
    test_db: Any, make_user: Any, pool_calls: list[str]
) -> None:
    await make_user("someone-here@example.com", password=PW)
    pool_calls.clear()

    with pytest.raises(HTTPException) as exc:
        await AuthService.login(
            LoginIn(email="someone-here@example.com", password="wrong-" + PW),
            test_db,
        )

    assert exc.value.status_code == 401
    assert pool_calls == ["_verify_login_password"]


def test_identity_warms_the_dummy_hash_at_startup() -> None:
    from unittest.mock import Mock

    from sandloom_identity import register

    recorder = Mock()
    register(recorder)
    tasks = [c.args[0] for c in recorder.add_startup_task.call_args_list]
    assert AuthService.warm_dummy_password_hash in tasks


# --- Same externally timed work for known and unknown emails ----------------

#: Response headers that legitimately differ per request.
_VOLATILE_HEADERS = {"date", "x-request-id", "x-process-time", "traceparent"}


class _RecordingCache:
    """Proxy over the lockout cache: records each command and its key SHAPES.

    Keys carry the subject hash, which differs per email by construction, so
    the trace keeps the prefix and replaces each hex digest by ``<h>``.
    """

    def __init__(self, inner: Any, trace: list[tuple[Any, ...]]) -> None:
        self._inner = inner
        self._trace = trace

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        async def call(*args: Any, **kwargs: Any) -> Any:
            self._trace.append(("redis", name, _shape(args), sorted(kwargs)))
            return await target(*args, **kwargs)

        return call


def _shape(value: Any) -> Any:
    import re

    if isinstance(value, str):
        return re.sub(r"[0-9a-f]{32,}", "<h>", value)
    if isinstance(value, list | tuple):
        return tuple(_shape(v) for v in value)
    return type(value).__name__


@pytest.fixture
def work_trace(monkeypatch: pytest.MonkeyPatch) -> Any:
    """One ordered trace of a login's Redis, SQL, transaction, pool and
    audit work (every engine: the real request UoW uses the app's own)."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    from sandloom_identity import lockout

    trace: list[tuple[Any, ...]] = []
    real_cache = lockout.cache
    monkeypatch.setattr(
        lockout, "cache", lambda *a, **k: _RecordingCache(real_cache(*a, **k), trace)
    )

    original_pool = auth_service.run_password_hashing

    async def pool(fn: Callable[..., Any], *args: Any) -> Any:
        trace.append(("pool", getattr(fn, "__name__", repr(fn))))
        return await original_pool(fn, *args)

    monkeypatch.setattr(auth_service, "run_password_hashing", pool)

    original_audit = auth_service.record_audit

    def audit(action: str, **kwargs: Any) -> Any:
        trace.append(("audit", action, sorted(kwargs)))
        return original_audit(action, **kwargs)

    monkeypatch.setattr(auth_service, "record_audit", audit)

    def on_sql(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        trace.append(("sql", " ".join(statement.split())))

    listeners: list[tuple[str, Callable[..., Any]]] = [
        ("before_cursor_execute", on_sql)
    ]
    for name in ("begin", "commit", "rollback"):
        listeners.append((name, lambda _conn, _n=name: trace.append(("tx", _n))))
    # event.remove leaves Engine._has_events set: restore it, or later tests
    # that pin the event-free fast path (test_tenant_rls_paths) fail.
    monkeypatch.setattr(Engine, "_has_events", Engine._has_events)
    for name, fn in listeners:
        event.listen(Engine, name, fn)
    yield trace
    for name, fn in listeners:
        event.remove(Engine, name, fn)


async def _committed_user(engine: Any, email: str) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from sandloom_identity.models.user import User

    async with AsyncSession(engine) as session:
        session.add(
            User(
                email=email,
                full_name="Timing",
                hashed_password=AuthService.get_password_hash(PW),
                is_active=True,
                is_verified=True,
            )
        )
        await session.commit()


async def _attempt(client: Any, email: str, trace: list[Any]) -> tuple[Any, ...]:
    trace.clear()
    response = await client.post(
        "/auth/login", json={"email": email, "password": "wrong-" + PW}
    )
    headers = {
        k: v for k, v in response.headers.items() if k.lower() not in _VOLATILE_HEADERS
    }
    return response.status_code, response.json(), headers, list(trace)


@pytest.mark.asyncio
async def test_wrong_password_and_unknown_email_do_identical_work(
    real_uow_client: Any, test_engine: Any, work_trace: list[Any]
) -> None:
    """Each request on its OWN request UoW, as in production: the lock's
    audit INSERT and COMMIT are part of the compared sequence."""
    from sandloom_identity import lockout

    client = real_uow_client
    await _committed_user(test_engine, "known-timing@example.com")
    # Warm-up: the app's first request opens its pool connections.
    await _attempt(client, "warm-up@example.com", work_trace)

    # Every failed attempt up to and including the one that locks.
    for attempt in range(1, lockout.FAIL_THRESHOLD + 1):
        known = await _attempt(client, "known-timing@example.com", work_trace)
        unknown = await _attempt(client, "unknown-timing@example.com", work_trace)

        assert known[0] == 401, (attempt, known)
        assert known[:3] == unknown[:3], attempt  # status, body, headers
        assert known[3] == unknown[3], attempt  # the whole work sequence
        kinds = [entry[0] for entry in known[3]]
        assert kinds.count("pool") == 1
        # The verify is queued before the lookup statement is sent.
        assert kinds.index("pool") < kinds.index("sql")
        if attempt == lockout.FAIL_THRESHOLD:
            # The lock is audited and COMMITTED for both.
            assert "audit" in kinds
            assert ("tx", "commit") in known[3]
            assert any(
                "INSERT INTO audit_log" in e[1] for e in known[3] if e[0] == "sql"
            )
        else:
            assert "audit" not in kinds
            assert ("tx", "commit") not in known[3]

    # Locked now: both answer the same 429.
    known = await _attempt(client, "known-timing@example.com", work_trace)
    unknown = await _attempt(client, "unknown-timing@example.com", work_trace)
    assert known[0] == 429
    assert known == unknown


@pytest.mark.asyncio
async def test_failed_lookup_releases_the_pool_thread(
    test_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verify is queued before the lookup: a lookup that fails must not
    leave a pool thread parked waiting for a hash that never comes."""
    import asyncio

    started: list[Any] = []
    original_pool = auth_service.run_password_hashing

    async def pool(fn: Callable[..., Any], *args: Any) -> Any:
        task = asyncio.ensure_future(original_pool(fn, *args))
        started.append(task)
        return await task

    monkeypatch.setattr(auth_service, "run_password_hashing", pool)

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(test_db, "execute", boom)
    with pytest.raises(RuntimeError):
        await AuthService.login(
            LoginIn(email="whoever@example.com", password=PW), test_db
        )
    assert len(started) == 1
    assert await asyncio.wait_for(started[0], timeout=5) is False
