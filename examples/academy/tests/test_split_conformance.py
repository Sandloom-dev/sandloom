"""Split conformance of the ENROLL saga (ADR-0008 §13): one script, two
deployments, the same terminal state.

* **monolith** — payments, notifications and enrollment in one process;
* **split** — enrollment alone in one process (its own database), payments +
  notifications in another (their own database), wired by
  ``SERVICE_BINDINGS`` over the real internal RPC (per-pair request MACs,
  signed commands — ADR-0004).

Each process runs its own lifespan: the advance consumer
``enrollment:sagas@advance`` on the in-process bus and the lease-guarded
timer sweep. The script tops up a balance, starts one enrollment the balance
covers and one it does not, waits until both sagas settle, and reads the
balance and the inbox. Both deployments must answer identically: the first
saga ``completed`` (seat ``active``, charged once, one mail), the second
``compensated`` (seat ``cancelled``, nothing charged).

Skips without PostgreSQL / Redis unless ``SANDLOOM_E2E_REQUIRED=1`` (CI).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from sandloom.sdk.security import (  # sandloom: advanced RPC keys of the split deployment
    generate_private_key,
    generate_x25519_key,
    private_pem,
    public_pem,
    x25519_private_pem,
    x25519_public_pem,
)
from sandloom.sdk.testing import (  # sandloom: advanced served processes
    ReservedPort,
    SpawnedServer,
    SpawnedServerError,
    as_user,
    probe,
    spawn_served,
)

pytestmark = [pytest.mark.slow, pytest.mark.split_conformance]

APP = Path(__file__).resolve().parents[1]
RUN = uuid.uuid4().hex[:8]
PAYMENTS = "academy.modules.payments"
NOTIFICATIONS = "academy.modules.notifications"
ENROLLMENT = "academy.modules.enrollment"
PROVIDED = {PAYMENTS: "payments.charges.v1", NOTIFICATIONS: "notifications.mail.v1"}
SETTLED = ("completed", "compensated", "parked")
STARTUP_TIMEOUT = 60.0
SETTLE_TIMEOUT = 45.0


def _unavailable(reason: str) -> None:
    if os.environ.get("SANDLOOM_E2E_REQUIRED", "").strip() == "1":
        pytest.fail(f"split conformance infrastructure unavailable: {reason}")
    pytest.skip(reason)


def _reachable(url: str, default_port: int) -> bool:
    parsed = urlparse(url.replace("+asyncpg", ""))
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or default_port), timeout=1.5
        ):
            return True
    except OSError:
        return False


def _db_url(base: str, name: str) -> str:
    return urlparse(base)._replace(path=f"/{name}").geturl()


async def _admin(base: str, statements: list[str]) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        _db_url(base, "postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


@dataclass
class Proc:
    deployment: str
    modules: tuple[str, ...]
    db_name: str
    listener: ReservedPort = field(default_factory=ReservedPort)
    served: SpawnedServer | None = None
    log_path: Path | None = None

    @property
    def label(self) -> str:
        return "+".join(m.rsplit(".", 1)[-1] for m in self.modules)

    @property
    def service_id(self) -> str:
        return f"{self.deployment}-{self.label}"

    @property
    def url(self) -> str:
        return self.listener.url

    def logs(self) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""
        return self.log_path.read_text(errors="replace")[-6000:]


@dataclass
class Deployment:
    name: str
    procs: list[Proc]

    def host(self, module: str) -> Proc:
        return next(p for p in self.procs if module in p.modules)


_KEYS: dict[str, tuple[str, str, str, str]] = {}


def _keys(service_id: str) -> tuple[str, str, str, str]:
    """``(ed25519 private, public, x25519 private, public)`` of one service."""
    if service_id not in _KEYS:
        signing = generate_private_key("EdDSA")
        dh = generate_x25519_key()
        _KEYS[service_id] = (
            private_pem(signing),
            public_pem(signing),
            x25519_private_pem(dh),
            x25519_public_pem(dh),
        )
    return _KEYS[service_id]


def _bindings(proc: Proc, dep: Deployment) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for peer in dep.procs:
        if peer is proc:
            continue
        for module in peer.modules:
            if module in PROVIDED:
                out[PROVIDED[module]] = {
                    "base_url": peer.url,
                    "service_id": peer.service_id,
                }
    return out


def _env(base: dict[str, str], dep: Deployment, proc: Proc) -> dict[str, str]:
    allowed = {
        p.service_id: sorted(_bindings(p, dep)) for p in dep.procs if _bindings(p, dep)
    }
    env = {
        **os.environ,
        **base,
        "ENVIRONMENT": "test",
        "SANDLOOM_COMPOSITION_ROOT": "academy.asgi",
        "SANDLOOM_MODULES": json.dumps(list(proc.modules)),
        "DATABASE_URL": _db_url(base["DATABASE_URL"], proc.db_name),
        "SERVICE_BINDINGS": json.dumps(_bindings(proc, dep)),
        "SANDLOOM_SERVICE_NAME": proc.service_id,
        "EVENT_BUS_BACKEND": "memory",
        "SERVICE_RPC_TIMEOUT_SECONDS": "10",
        # retries / deadlines are driven by the sweep: keep it brisk
        "SAGA_SWEEP_INTERVAL_SECONDS": "0.5",
        "SAGA_RETRY_MAX_SECONDS": "1",
        "RATE_LIMIT_ENABLED": "false",
        "LOG_FORMAT": "text",
        "PYTHONPATH": os.pathsep.join([str(APP), os.environ.get("PYTHONPATH", "")]),
        "RPC_COMMANDS_REQUIRE_SIGNATURE": "on",
        "RPC_SERVICE_ID": proc.service_id,
        "RPC_PRIVATE_KEY": _keys(proc.service_id)[0],
        "RPC_KEY_ID": "k1",
        "RPC_SERVICE_KEYS": json.dumps(
            {p.service_id: {"k1": _keys(p.service_id)[1]} for p in dep.procs}
        ),
        "RPC_ALLOWED_CALLS": json.dumps(allowed),
        "RPC_DH_PRIVATE_KEY": _keys(proc.service_id)[2],
        "RPC_DH_KEY_ID": "d1",
        "RPC_SERVICE_DH_KEYS": json.dumps(
            {p.service_id: {"d1": _keys(p.service_id)[3]} for p in dep.procs}
        ),
    }
    for key in ("MODULE_REQUIRED", "INFRA"):
        env.pop(key, None)
    return env


def _migrate(env: dict[str, str]) -> None:
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("sandloom")),
            "migrate",
            "upgrade-all",
            "--apply",
        ],
        cwd=APP,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]


def _wait_ready(procs: list[Proc]) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    pending = list(procs)
    while pending and time.monotonic() < deadline:
        for proc in list(pending):
            assert proc.served is not None
            try:
                ready = probe(proc.served)
            except SpawnedServerError as exc:
                raise AssertionError(f"{exc}\n{proc.logs()}") from None
            if ready:
                pending.remove(proc)
        time.sleep(0.2)
    if pending:
        raise AssertionError(f"did not start: {pending[0].label}\n{pending[0].logs()}")


@pytest.fixture(scope="module")
def deployments(tmp_path_factory: pytest.TempPathFactory) -> Iterator[list[Deployment]]:
    from sandloom.sdk.infra import settings  # sandloom: advanced the test environment

    base = {
        "DATABASE_URL": str(settings.database_url),
        "CACHE_URL": str(settings.cache_url),
        "BROKER_URL": str(settings.broker_url),
        "JWT_SECRET": settings.jwt_secret.get_secret_value()
        if hasattr(settings.jwt_secret, "get_secret_value")
        else str(settings.jwt_secret),
    }
    if not _reachable(base["DATABASE_URL"], 5432):
        _unavailable("PostgreSQL not reachable")
    if not _reachable(base["CACHE_URL"], 6379):
        _unavailable("Redis not reachable")
    deps = [
        Deployment(
            "monolith",
            [
                Proc(
                    "monolith",
                    (PAYMENTS, NOTIFICATIONS, ENROLLMENT),
                    f"academy_{RUN}_mono",
                )
            ],
        ),
        Deployment(
            "split",
            [
                Proc("split", (ENROLLMENT,), f"academy_{RUN}_enrollment"),
                Proc("split", (PAYMENTS, NOTIFICATIONS), f"academy_{RUN}_providers"),
            ],
        ),
    ]
    procs = [p for d in deps for p in d.procs]
    names = [p.db_name for p in procs]
    asyncio.run(
        _admin(
            base["DATABASE_URL"],
            [f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)' for n in names]
            + [f'CREATE DATABASE "{n}"' for n in names],
        )
    )
    log_dir = tmp_path_factory.mktemp("academy-split")
    try:
        for dep in deps:
            for proc in dep.procs:
                env = _env(base, dep, proc)
                _migrate(env)
                proc.log_path = log_dir / f"{proc.service_id}.log"
                with proc.log_path.open("wb") as log:
                    proc.served = spawn_served(
                        proc.listener,
                        "academy.asgi:app",
                        env=env,
                        label=proc.service_id,
                        cwd=APP,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
        _wait_ready(procs)
        yield deps
    finally:
        for proc in procs:
            if proc.served is not None:
                proc.served.stop()
            proc.listener.close()
        asyncio.run(
            _admin(
                base["DATABASE_URL"],
                [f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)' for n in names],
            )
        )


async def _settle(
    http: httpx.AsyncClient, url: str, headers: dict[str, str], proc: Proc
) -> dict[str, Any]:
    deadline = time.monotonic() + SETTLE_TIMEOUT
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = (await http.get(url, headers=headers)).json()
        # settled = a terminal saga AND a terminal enrollment (a torn read
        # of the two would otherwise pass as "settled")
        saga = body.get("saga_status")
        if saga == "parked" or (
            saga in SETTLED and body.get("status") in ("active", "cancelled")
        ):
            return body
        await asyncio.sleep(0.2)
    raise AssertionError(f"saga did not settle: {body}\n{proc.logs()}")


async def _script(dep: Deployment) -> dict[str, Any]:
    _, headers = as_user()
    enrollment = dep.host(ENROLLMENT)
    payments = dep.host(PAYMENTS)
    notifications = dep.host(NOTIFICATIONS)
    async with httpx.AsyncClient(timeout=20) as http:
        topup = await http.post(
            f"{payments.url}/api/v1/payments/topup",
            json={"amount": 100},
            headers=headers,
        )
        assert topup.status_code == 200, topup.text
        settled: dict[str, Any] = {}
        for label, price in (("covered", 30), ("short", 500)):
            started = await http.post(
                f"{enrollment.url}/api/v1/enrollments",
                json={"course_id": str(uuid.uuid4()), "price": price},
                headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
            )
            assert started.status_code == 202, started.text
            url = f"{enrollment.url}{started.json()['status_url']}"
            body = await _settle(http, url, headers, enrollment)
            settled[label] = {k: body[k] for k in ("status", "saga_status", "price")}
        balance = (
            await http.get(f"{payments.url}/api/v1/payments/balance", headers=headers)
        ).json()["balance"]
        inbox = (
            await http.get(
                f"{notifications.url}/api/v1/notifications/inbox", headers=headers
            )
        ).json()
    return {**settled, "balance": balance, "inbox": [m["template"] for m in inbox]}


EXPECTED = {
    "covered": {"status": "active", "saga_status": "completed", "price": 30},
    "short": {"status": "cancelled", "saga_status": "compensated", "price": 500},
    "balance": 70,
    "inbox": ["enrolled"],
}


async def test_monolith_and_split_reach_the_same_terminal_state(
    deployments: list[Deployment],
) -> None:
    results = {dep.name: await _script(dep) for dep in deployments}
    assert results["monolith"] == EXPECTED, results
    assert results["split"] == results["monolith"], results


async def test_the_split_enrollment_service_calls_its_providers_remotely(
    deployments: list[Deployment],
) -> None:
    split = next(d for d in deployments if d.name == "split")
    enrollment = split.host(ENROLLMENT)
    env_bindings = _bindings(enrollment, split)
    assert sorted(env_bindings) == ["notifications.mail.v1", "payments.charges.v1"]
    assert all(b["base_url"] != enrollment.url for b in env_bindings.values())
