"""IDEMPOTENCY_CACHE_INSTANCE — idempotency records on their own cache.

* unset (default): ``idempotency_guard`` records and task-enqueue dedupe live
  in the security cache (``SECURITY_CACHE_INSTANCE``), exactly as before;
* set: every reserve / replay / conflict / release lands in that instance and
  never in the security one, while revocation marks stay in the security
  cache; a ``fail_closed`` guard still answers 503 when it is down;
* ``multi_tenant_strict``: the instance must be declared on its own Redis
  server (config) and attested as a separate Redis process (startup).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from sandloom.core import idempotency, production_profile
from sandloom.core.app_factory import ConfigurationError
from sandloom.core.auth import revocation
from sandloom.core.config.settings import settings
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.idempotency import replay as idempotency_replay
from sandloom.core.infra import access
from sandloom.core.migrations import backup
from sandloom.core.tasks import enqueue
from sandloom.core.testing import InMemoryCache, make_user_headers
from sandloom.core.tests.test_production_profile import _COMPLIANT_INFRA, _settings
from sandloom.shared.guards.auth import get_current_user_id


class _Caches:
    """One :class:`InMemoryCache` per instance name; records each lookup."""

    def __init__(self) -> None:
        self.instances: dict[str, InMemoryCache] = {}
        self.names: list[str] = []

    def __call__(self, name: str = "default") -> InMemoryCache:
        self.names.append(name)
        return self.instances.setdefault(name, InMemoryCache())

    def keys(self, name: str) -> list[str]:
        return sorted(self.instances[name].store) if name in self.instances else []


@pytest.fixture
def caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Caches]:
    routed = _Caches()
    monkeypatch.setattr(access, "cache", routed)
    monkeypatch.setattr(idempotency_cache_store, "cache", routed)
    monkeypatch.setattr(settings, "security_cache_instance", "security")
    monkeypatch.setattr(settings, "idempotency_cache_instance", "")
    yield routed


class _Body(BaseModel):
    amount: int


def _app(*, fail_closed: bool = False) -> tuple[FastAPI, list[int]]:
    app = FastAPI()
    app.add_middleware(idempotency.IdempotencyCaptureMiddleware)
    app.add_exception_handler(
        idempotency_replay._ReplayResponse, idempotency_replay._replay_handler
    )
    runs: list[int] = []

    @app.post("/charge")
    async def _charge(
        body: _Body,
        user_id: str = Depends(get_current_user_id),
        _guard: None = Depends(
            idempotency.idempotency_guard(scope="charges", fail_closed=fail_closed)
        ),
    ) -> dict[str, int]:
        runs.append(body.amount)
        if body.amount < 0:
            raise HTTPException(400, "negative")
        return {"charged": body.amount, "run": len(runs)}

    return app, runs


async def _post(app: FastAPI, headers: dict[str, str], amount: int) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as http:
        return await http.post("/charge", headers=headers, json={"amount": amount})


def _headers(key: str) -> dict[str, str]:
    _uid, headers = make_user_headers()
    return {**headers, "Idempotency-Key": key}


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def test_unset_follows_the_security_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "idempotency_cache_instance", "")
    monkeypatch.setattr(settings, "security_cache_instance", "security")
    assert access.idempotency_cache_name() == "security"
    monkeypatch.setattr(settings, "security_cache_instance", "default")
    assert access.idempotency_cache_name() == "default"
    monkeypatch.setattr(settings, "idempotency_cache_instance", "idempotency")
    assert access.idempotency_cache_name() == "idempotency"
    assert access.security_cache_name() == "default"  # never moves


# --------------------------------------------------------------------------- #
# idempotency_guard
# --------------------------------------------------------------------------- #
async def test_default_keeps_guard_records_in_the_security_cache(
    caches: _Caches,
) -> None:
    app, runs = _app()
    headers = _headers("k-default")
    first = await _post(app, headers, 5)
    second = await _post(app, headers, 5)
    assert first.status_code == second.status_code == 200
    assert second.headers.get("Idempotent-Replay") == "true"
    assert runs == [5]
    assert caches.keys("security")
    assert "idempotency" not in caches.instances


async def test_reserve_replay_conflict_release_use_the_idempotency_instance(
    caches: _Caches, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "idempotency_cache_instance", "idempotency")
    app, runs = _app()
    headers = _headers("k-separate")

    first = await _post(app, headers, 5)
    assert first.status_code == 200
    stored = caches.keys("idempotency")
    assert stored, "the reservation / completed record lives in the instance"
    replay = await _post(app, headers, 5)
    assert replay.status_code == 200
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json() == first.json()
    conflict = await _post(app, headers, 7)
    assert conflict.status_code == 422
    assert runs == [5]

    # A failed request releases its reservation (in the instance): the
    # retry with the same key runs afresh.
    failing = _headers("k-release")
    assert (await _post(app, failing, -1)).status_code == 400
    assert caches.keys("idempotency") == stored
    assert (await _post(app, failing, -1)).status_code == 400
    assert runs == [5, -1, -1]

    # Revocation marks were read from the security cache; no idempotency
    # record ever landed there.
    assert "security" in caches.names
    assert not [k for k in caches.keys("security") if k.startswith("idem")]
    assert caches.keys("security") == []


async def test_revocation_marks_stay_in_the_security_cache(
    caches: _Caches, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "idempotency_cache_instance", "idempotency")
    await revocation.revoke_user_tokens(uuid.uuid4())
    assert caches.keys("security")
    assert caches.keys("idempotency") == []


@pytest.mark.parametrize("fail_closed", [False, True])
async def test_fail_closed_follows_the_idempotency_instance(
    caches: _Caches, monkeypatch: pytest.MonkeyPatch, fail_closed: bool
) -> None:
    monkeypatch.setattr(settings, "idempotency_cache_instance", "idempotency")
    caches.instances["idempotency"] = InMemoryCache(broken=True)
    app, runs = _app(fail_closed=fail_closed)
    resp = await _post(app, _headers("k-down"), 5)
    assert resp.status_code == (503 if fail_closed else 200)
    assert runs == ([] if fail_closed else [5])


# --------------------------------------------------------------------------- #
# Task-enqueue dedupe and the contract-command cache fallback
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("instance", ["", "idempotency"])
async def test_task_enqueue_dedupe_follows_the_setting(
    caches: _Caches, monkeypatch: pytest.MonkeyPatch, instance: str
) -> None:
    monkeypatch.setattr(settings, "idempotency_cache_instance", instance)
    sent: list[str] = []

    async def send(task_id: str) -> str:
        sent.append(task_id)
        return task_id

    kwargs: dict[str, Any] = {
        "namespace": "probe",
        "key": "op-1",
        "task": "probe.task",
        "payload": {"x": 1},
        "delay_seconds": None,
        "ttl_seconds": 60,
        "send": send,
    }
    first = await enqueue.enqueue_once(**kwargs)
    assert await enqueue.enqueue_once(**kwargs) == first
    assert len(sent) == 1
    expected = instance or "security"
    other = "security" if instance else "idempotency"
    assert caches.keys(expected)
    assert caches.keys(other) == []


async def test_contract_command_cache_fallback_follows_the_setting(
    caches: _Caches, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "idempotency_cache_instance", "idempotency")
    outcome, _ = await idempotency.reserve_key("idem:contract:x", "fp", 60)
    assert outcome == "reserved"
    assert caches.keys("idempotency") == ["idem:contract:x"]
    assert caches.keys("security") == []


# --------------------------------------------------------------------------- #
# multi_tenant_strict
# --------------------------------------------------------------------------- #
_IDEM_INFRA = {
    **_COMPLIANT_INFRA,
    "cache": {
        **_COMPLIANT_INFRA["cache"],
        "idempotency": {"driver": "redis", "url": "redis://idem-redis:6379/0"},
    },
}


def test_strict_profile_accepts_a_separate_idempotency_redis() -> None:
    cfg = _settings(INFRA=_IDEM_INFRA, IDEMPOTENCY_CACHE_INSTANCE="idempotency")
    assert cfg.idempotency_cache_instance == "idempotency"
    errors, warnings = production_profile.config_problems(cfg)
    assert errors == [] and warnings == []


def test_strict_profile_default_is_unchanged() -> None:
    cfg = _settings()
    assert cfg.idempotency_cache_instance == ""
    assert production_profile.config_problems(cfg) == ([], [])
    # Naming the security instance explicitly is the same as leaving it unset.
    cfg = _settings(IDEMPOTENCY_CACHE_INSTANCE="security")
    assert production_profile.config_problems(cfg) == ([], [])


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"IDEMPOTENCY_CACHE_INSTANCE": "default"},
            "IDEMPOTENCY_CACHE_INSTANCE is 'default'",
        ),
        (
            {"IDEMPOTENCY_CACHE_INSTANCE": "idem"},
            "IDEMPOTENCY_CACHE_INSTANCE='idem' is not declared",
        ),
        (
            {
                "IDEMPOTENCY_CACHE_INSTANCE": "idempotency",
                "INFRA": {
                    **_COMPLIANT_INFRA,
                    "cache": {
                        **_COMPLIANT_INFRA["cache"],
                        "idempotency": {"url": "redis://cache-redis:6379/7"},
                    },
                },
            },
            "cache/idempotency runs on the same Redis server as the default cache",
        ),
    ],
)
def test_strict_profile_refuses_an_unsafe_idempotency_instance(
    overrides: dict[str, Any], reason: str
) -> None:
    with pytest.raises(
        ConfigurationError, match="multi_tenant_strict is not met"
    ) as exc:
        _settings(**overrides)
    assert reason in str(exc.value)


def test_strict_profile_warns_when_sharing_the_security_redis() -> None:
    infra = {
        **_COMPLIANT_INFRA,
        "cache": {
            **_COMPLIANT_INFRA["cache"],
            "idempotency": {"url": "redis://sec-redis:6379/3"},
        },
    }
    cfg = _settings(INFRA=infra, IDEMPOTENCY_CACHE_INSTANCE="idempotency")
    errors, warnings = production_profile.config_problems(cfg)
    assert errors == []
    assert any("same Redis server as cache/security" in w for w in warnings)


def _runtime_core(cfg: Any, identities: dict[str, str]) -> Any:
    def provider(_capability: str, name: str) -> Any:
        return SimpleNamespace(
            deployment_identity=AsyncMock(return_value=identities[name])
        )

    return SimpleNamespace(settings=cfg, infra=SimpleNamespace(provider=provider))


async def test_strict_runtime_attests_the_idempotency_redis_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        production_profile, "verify_tenant_rls_coverage", AsyncMock(return_value=[])
    )
    cfg = _settings(INFRA=_IDEM_INFRA, IDEMPOTENCY_CACHE_INSTANCE="idempotency")
    await production_profile.enforce_runtime(
        _runtime_core(cfg, {"default": "a", "security": "b", "idempotency": "c"})
    )
    with pytest.raises(
        production_profile.ProductionProfileError,
        match="Idempotency cache and default cache reached the same Redis process",
    ):
        await production_profile.enforce_runtime(
            _runtime_core(cfg, {"default": "a", "security": "b", "idempotency": "a"})
        )


def test_backup_plan_labels_the_idempotency_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "security_cache_instance", "security")
    monkeypatch.setattr(settings, "idempotency_cache_instance", "idempotency")
    config = {
        "cache": {
            name: SimpleNamespace(options={"url": f"redis://{name}:6379/0"})
            for name in ("default", "security", "idempotency")
        }
    }
    roles = backup._redis_roles(config, settings)
    assert roles["redis://security:6379/0"] == [
        "cache:security [SECURITY: revocation marks, user blocks]"
    ]
    assert "IDEMPOTENCY" in roles["redis://idempotency:6379/0"][0]
