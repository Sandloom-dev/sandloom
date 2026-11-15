"""The kernel's security and idempotency duties on ``atomic_store`` (by role).

* ``INFRA.atomic_store.security`` holds revocation marks, blocks and RPC
  nonces; ``INFRA.atomic_store.idempotency`` holds the idempotency guard's
  records, enqueue dedupe and the contract-command fallback, and is the
  ``security`` instance when absent (spec 4, "Instances");
* ``SECURITY_CACHE_INSTANCE`` and ``IDEMPOTENCY_CACHE_INSTANCE`` are removed:
  a leftover setting fails startup with the fix;
* ``multi_tenant_strict``: the instances are declared on their own Redis
  server (config) and attested through ``isolation_identity()`` (startup);
* a production deployment without ``INFRA.atomic_store.security`` is refused,
  and development builds one on the cache's Redis only outside production.
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
from pydantic import BaseModel, ValidationError
from sandloom_redis.atomic_store import RedisAtomicStoreProvider

from sandloom.core import idempotency, production_profile
from sandloom.core.app_factory.config import configuration_problems
from sandloom.core.auth import revocation
from sandloom.core.config.settings import Settings, settings
from sandloom.core.drivers.redis_cache import RedisCacheProvider
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.idempotency import replay as idempotency_replay
from sandloom.core.infra import access
from sandloom.core.infra.config import load_infra_config
from sandloom.core.migrations import backup
from sandloom.core.tasks import enqueue
from sandloom.core.testing import InMemoryAtomicStore, make_user_headers
from sandloom.core.tests.test_production_profile import (
    _BASE,
    _COMPLIANT_INFRA,
    _REFUSED,
    _settings,
)
from sandloom.shared.guards.auth import get_current_user_id


class _Stores:
    """One :class:`InMemoryAtomicStore` per role; records each lookup."""

    def __init__(self) -> None:
        self.instances: dict[str, InMemoryAtomicStore] = {}
        self.roles: list[str] = []

    def __call__(self, role: str = "security") -> InMemoryAtomicStore:
        self.roles.append(role)
        return self.instances.setdefault(role, InMemoryAtomicStore())

    def keys(self, role: str) -> list[str]:
        return sorted(self.instances[role].store) if role in self.instances else []


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Stores]:
    """Revocation and the guard resolve a role; ``idempotency`` is its own
    instance here (the fallback to ``security`` is tested separately)."""
    routed = _Stores()
    monkeypatch.setattr(access, "atomic_store", routed)
    monkeypatch.setattr(idempotency_cache_store, "atomic_store", routed)
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
# Roles
# --------------------------------------------------------------------------- #
class _Registry:
    def __init__(self, declared: set[str]) -> None:
        self.declared = declared
        self.asked: list[str] = []

    def instance_config(self, capability: str, name: str) -> object | None:
        assert capability == "atomic_store"
        return object() if name in self.declared else None

    def provider(self, capability: str, name: str) -> str:
        assert capability == "atomic_store"
        self.asked.append(name)
        return name


@pytest.mark.parametrize(
    ("declared", "idempotency_instance"),
    [({"security"}, "security"), ({"security", "idempotency"}, "idempotency")],
)
def test_an_absent_idempotency_instance_is_the_security_one(
    monkeypatch: pytest.MonkeyPatch, declared: set[str], idempotency_instance: str
) -> None:
    registry = _Registry(declared)
    monkeypatch.setattr(access, "get_active_infra", lambda: registry)
    assert access.atomic_store("security") == "security"  # type: ignore[comparison-overlap]
    assert access.atomic_store("idempotency") == idempotency_instance  # type: ignore[comparison-overlap]


# --------------------------------------------------------------------------- #
# idempotency_guard
# --------------------------------------------------------------------------- #
async def test_guard_records_live_in_the_idempotency_role(stores: _Stores) -> None:
    app, runs = _app()
    headers = _headers("k-separate")

    first = await _post(app, headers, 5)
    assert first.status_code == 200
    stored = stores.keys("idempotency")
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
    assert stores.keys("idempotency") == stored
    assert (await _post(app, failing, -1)).status_code == 400
    assert runs == [5, -1, -1]

    # Revocation marks were read from the security instance; no idempotency
    # record ever landed there.
    assert "security" in stores.roles
    assert stores.keys("security") == []


async def test_revocation_marks_stay_in_the_security_role(stores: _Stores) -> None:
    await revocation.revoke_user_tokens(uuid.uuid4())
    assert stores.keys("security")
    assert stores.keys("idempotency") == []


@pytest.mark.parametrize("fail_closed", [False, True])
async def test_fail_closed_follows_the_idempotency_instance(
    stores: _Stores, fail_closed: bool
) -> None:
    stores.instances["idempotency"] = InMemoryAtomicStore(broken=True)
    app, runs = _app(fail_closed=fail_closed)
    resp = await _post(app, _headers("k-down"), 5)
    assert resp.status_code == (503 if fail_closed else 200)
    assert runs == ([] if fail_closed else [5])


async def test_a_reservation_is_one_add_or_get(stores: _Stores) -> None:
    """The guard's reservation is one ``add_or_get`` round trip, never ``add``
    then ``get``."""
    calls: list[str] = []
    store = stores("idempotency")
    original_add_or_get = store.add_or_get

    async def counting(*args: Any, **kwargs: Any) -> tuple[bool, str | None]:
        calls.append("add_or_get")
        return await original_add_or_get(*args, **kwargs)

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the reservation must be one add_or_get")

    store.add_or_get = counting  # type: ignore[method-assign]
    store.add = forbidden  # type: ignore[method-assign]
    store.get = forbidden  # type: ignore[method-assign]
    outcome, _ = await idempotency.reserve_key("idem:contract:one", "fp", 60)
    assert outcome == "reserved"
    assert calls == ["add_or_get"]


# --------------------------------------------------------------------------- #
# Task-enqueue dedupe and the contract-command fallback
# --------------------------------------------------------------------------- #
async def test_task_enqueue_dedupe_lives_in_the_idempotency_atomic_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held: dict[str, InMemoryAtomicStore] = {}
    monkeypatch.setattr(
        enqueue,
        "atomic_store",
        lambda role: held.setdefault(role, InMemoryAtomicStore()),
    )
    sent: list[str] = []

    async def send() -> str:
        sent.append("sent")
        return "task-1"

    kwargs: dict[str, Any] = {
        "namespace": "probe",
        "key": "op-1",
        "task": "probe.task",
        "payload": b'{"x":1}',
        "delay": None,
        "ttl_seconds": 60,
        "task_id": "task-1",
        "send": send,
    }
    first = await enqueue.enqueue_once(**kwargs)
    assert await enqueue.enqueue_once(**kwargs) == first
    assert len(sent) == 1
    assert sorted(held) == ["idempotency"]
    assert held["idempotency"].store


async def test_contract_command_cache_fallback_uses_the_idempotency_role(
    stores: _Stores,
) -> None:
    outcome, _ = await idempotency.reserve_key("idem:contract:x", "fp", 60)
    assert outcome == "reserved"
    assert stores.keys("idempotency") == ["idem:contract:x"]
    assert stores.keys("security") == []


# --------------------------------------------------------------------------- #
# Removed settings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("name", "fix"),
    [
        ("SECURITY_CACHE_INSTANCE", "INFRA.atomic_store.security"),
        ("IDEMPOTENCY_CACHE_INSTANCE", "INFRA.atomic_store.idempotency"),
    ],
)
def test_a_leftover_cache_instance_setting_fails_startup_with_the_fix(
    name: str, fix: str
) -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, **{name: "security"})  # type: ignore[call-arg]
    assert f"{name} was removed" in str(exc.value)
    assert fix in str(exc.value)


# --------------------------------------------------------------------------- #
# Development default, production refusal
# --------------------------------------------------------------------------- #
def test_development_builds_the_security_store_on_the_cache_redis_only_outside_prod() -> (
    None
):
    dev = Settings(  # type: ignore[call-arg]
        _env_file=None, ENVIRONMENT="dev", CACHE_URL="redis://cache-redis:6379/4"
    )
    config = load_infra_config(dev)
    security = config["atomic_store"]["security"]
    assert security.driver == "redis" and security.implicit
    assert security.options["url"] == "redis://cache-redis:6379/4"
    assert "idempotency" not in config["atomic_store"]


def test_production_never_builds_a_security_store_from_the_cache() -> None:
    prod = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ENVIRONMENT="prod",
        CACHE_URL="redis://cache-redis:6379/4",
        JWT_SECRET="9f3b7c1e5a2d4f6b8c0e1a3d5f7b9c2e4a6d8f0b1c3e5a7d9f2b4c6e8a0d1f3b",
    )
    assert "atomic_store" not in load_infra_config(prod)


def test_a_declared_security_store_gets_the_client_bounds_but_not_the_cache_url() -> (
    None
):
    cfg = Settings(  # type: ignore[call-arg]
        _env_file=None,
        ENVIRONMENT="dev",
        CACHE_URL="redis://cache-redis:6379/4",
        INFRA={"atomic_store": {"security": {"driver": "redis"}}},
    )
    security = load_infra_config(cfg)["atomic_store"]["security"]
    assert not security.implicit
    assert "url" not in security.options


# --------------------------------------------------------------------------- #
# multi_tenant_strict
# --------------------------------------------------------------------------- #
_IDEM_INFRA = {
    **_COMPLIANT_INFRA,
    "atomic_store": {
        **_COMPLIANT_INFRA["atomic_store"],
        "idempotency": {"driver": "redis", "url": "redis://idem-redis:6379/0"},
    },
}


def test_strict_profile_accepts_a_separate_idempotency_redis() -> None:
    cfg = _settings(INFRA=_IDEM_INFRA)
    assert production_profile.config_problems(cfg) == ([], [])


def test_strict_profile_default_is_unchanged() -> None:
    assert production_profile.config_problems(_settings()) == ([], [])


# --------------------------------------------------------------------------- #
# The production refusal holds on every profile (spec section 4)
# --------------------------------------------------------------------------- #
_STANDARD = {"PRODUCTION_PROFILE": "standard"}


def test_standard_profile_prod_without_a_security_store_is_refused() -> None:
    with pytest.raises(_REFUSED, match="atomic_store.*security.*is not declared"):
        _settings(**_STANDARD, INFRA={})


def test_standard_profile_prod_with_the_memory_security_store_is_refused() -> None:
    infra = {"atomic_store": {"security": {"driver": "memory"}}}
    with pytest.raises(_REFUSED, match="development-only memory driver"):
        _settings(**_STANDARD, INFRA=infra)


def test_standard_profile_prod_with_a_declared_security_store_boots() -> None:
    cfg = _settings(**_STANDARD, INFRA=_COMPLIANT_INFRA)
    assert cfg.production_profile == "standard"


def test_the_refusal_is_reported_once_under_the_strict_profile() -> None:
    cfg = Settings(_env_file=None, **{**_BASE, "INFRA": {}})  # type: ignore[call-arg]
    undeclared = [p for p in configuration_problems(cfg) if "is not declared" in p]
    assert len(undeclared) == 1


def test_development_and_test_keep_the_implicit_security_store() -> None:
    for environment in ("dev", "test"):
        cfg = _settings(ENVIRONMENT=environment, **_STANDARD, INFRA={})
        assert load_infra_config(cfg)["atomic_store"]["security"].implicit


@pytest.mark.parametrize(
    ("idempotency_options", "reason"),
    [
        (
            {"driver": "redis", "url": "redis://cache-redis:6379/7"},
            "atomic_store/idempotency runs on the same Redis server as the default",
        ),
        (
            {"driver": "memory"},
            'INFRA["atomic_store"]["idempotency"] uses the development-only memory',
        ),
    ],
)
def test_strict_profile_refuses_an_unsafe_idempotency_instance(
    idempotency_options: dict[str, Any], reason: str
) -> None:
    infra = {
        **_COMPLIANT_INFRA,
        "atomic_store": {
            **_COMPLIANT_INFRA["atomic_store"],
            "idempotency": idempotency_options,
        },
    }
    with pytest.raises(_REFUSED, match="multi_tenant_strict is not met") as exc:
        _settings(INFRA=infra)
    assert reason in str(exc.value)


def test_strict_profile_warns_when_sharing_the_security_redis() -> None:
    infra = {
        **_COMPLIANT_INFRA,
        "atomic_store": {
            **_COMPLIANT_INFRA["atomic_store"],
            "idempotency": {"driver": "redis", "url": "redis://sec-redis:6379/3"},
        },
    }
    cfg = _settings(INFRA=infra)
    errors, warnings = production_profile.config_problems(cfg)
    assert errors == []
    assert any("same Redis server as atomic_store/security" in w for w in warnings)


class _Provider:
    def __init__(
        self,
        identity: str | Exception | None,
        features: frozenset[str] = frozenset({"never_evicts", "shared"}),
    ) -> None:
        self._identity = identity
        self._features = features

    async def isolation_identity(self) -> str | None:
        if isinstance(self._identity, Exception):
            raise self._identity
        return self._identity

    def features(self) -> frozenset[str]:
        return self._features


def _runtime_core(
    cfg: Any, providers: dict[tuple[str, str], _Provider], declared: set[str]
) -> Any:
    def provider(capability: str, name: str) -> _Provider:
        return providers[(capability, name)]

    def instance_config(capability: str, name: str) -> object | None:
        return object() if (capability == "atomic_store" and name in declared) else None

    return SimpleNamespace(
        settings=cfg,
        infra=SimpleNamespace(provider=provider, instance_config=instance_config),
    )


@pytest.fixture
def no_rls_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        production_profile, "verify_tenant_rls_coverage", AsyncMock(return_value=[])
    )


async def test_strict_runtime_attests_each_instance_apart_from_the_cache(
    no_rls_problems: None,
) -> None:
    cfg = _settings(INFRA=_IDEM_INFRA)
    providers = {
        ("cache", "default"): _Provider("redis:a"),
        ("atomic_store", "security"): _Provider("redis:b"),
        ("atomic_store", "idempotency"): _Provider("redis:c"),
    }
    core = _runtime_core(cfg, providers, {"security", "idempotency"})
    await production_profile.enforce_runtime(core)
    providers[("atomic_store", "idempotency")] = _Provider("redis:a")
    with pytest.raises(
        production_profile.ProductionProfileError,
        match="idempotency atomic_store and the default cache share one failure",
    ):
        await production_profile.enforce_runtime(core)


@pytest.mark.parametrize(
    "identity",
    [None, "", RuntimeError("INFO refused")],
    ids=["none", "empty", "raises"],
)
async def test_an_unknown_or_failing_identity_fails_attestation(
    no_rls_problems: None, identity: str | Exception | None
) -> None:
    providers = {
        ("cache", "default"): _Provider("redis:a"),
        ("atomic_store", "security"): _Provider(identity),
    }
    core = _runtime_core(_settings(), providers, {"security"})
    with pytest.raises(
        production_profile.ProductionProfileError, match="Cannot attest the security"
    ):
        await production_profile.enforce_runtime(core)


async def test_a_cache_without_an_identity_fails_attestation(
    no_rls_problems: None,
) -> None:
    providers = {
        ("cache", "default"): _Provider(None),
        ("atomic_store", "security"): _Provider("redis:b"),
    }
    core = _runtime_core(_settings(), providers, {"security"})
    with pytest.raises(
        production_profile.ProductionProfileError, match="Cannot attest the security"
    ):
        await production_profile.enforce_runtime(core)


async def test_strict_profile_attests_real_redis_identity(
    no_rls_problems: None,
) -> None:
    """The real classes on the test Redis: the cache and the Redis atomic store
    name their server alike (``redis:<run_id>``), so a store on the cache's
    server is refused, and a store whose server differs is attested (spec 12)."""
    url = settings.cache_url
    store = RedisAtomicStoreProvider(
        name="security", driver="redis", options={"url": url}
    )
    cache = RedisCacheProvider(
        name="default", driver="redis", options={"url": url}, shared=False
    )
    try:
        identity = await store.isolation_identity()
        assert identity is not None and identity.startswith("redis:")
        assert await cache.isolation_identity() == identity

        providers: dict[tuple[str, str], Any] = {
            ("cache", "default"): cache,
            ("atomic_store", "security"): store,
        }
        core = _runtime_core(_settings(), providers, {"security"})
        with pytest.raises(
            production_profile.ProductionProfileError,
            match="share one failure domain",
        ):
            await production_profile.enforce_runtime(core)

        providers[("cache", "default")] = _Provider("redis:another-server")
        await production_profile.enforce_runtime(core)
    finally:
        await store.aclose()
        await cache.aclose()


@pytest.mark.parametrize("missing", ["never_evicts", "shared"])
async def test_a_security_store_must_declare_never_evicts_and_shared(
    no_rls_problems: None, missing: str
) -> None:
    features = frozenset({"never_evicts", "shared"} - {missing})
    providers = {
        ("cache", "default"): _Provider("redis:a"),
        ("atomic_store", "security"): _Provider("redis:b", features),
    }
    core = _runtime_core(_settings(), providers, {"security"})
    with pytest.raises(production_profile.ProductionProfileError, match=missing):
        await production_profile.enforce_runtime(core)


async def test_the_in_memory_store_expires_a_row_after_its_ttl() -> None:
    now = [100.0]
    mem = InMemoryAtomicStore()
    mem.clock = lambda: now[0]
    assert await mem.add("k", "v", ttl_seconds=10)
    assert await mem.get("k") == "v"
    now[0] = 109.9
    assert await mem.add_or_get("k", "w", ttl_seconds=10) == (False, "v")
    now[0] = 110.0
    assert await mem.get("k") is None
    assert await mem.add("k", "w", ttl_seconds=10)  # free again after expiry
    await mem.set("forever", "v")
    now[0] = 10_000.0
    assert await mem.get("forever") == "v"
    assert await mem.get("k") is None


def test_backup_plan_labels_the_idempotency_redis() -> None:
    config = {
        "cache": {
            "default": SimpleNamespace(options={"url": "redis://default:6379/0"})
        },
        "atomic_store": {
            name: SimpleNamespace(
                driver="redis", options={"url": f"redis://{name}:6379/0"}
            )
            for name in ("security", "idempotency")
        },
    }
    roles = backup._redis_roles(config, SimpleNamespace(cache_url="redis://x"))
    assert roles["redis://security:6379/0"] == [
        "atomic_store:security [SECURITY: revocation marks, user blocks, RPC nonces]"
    ]
    assert "IDEMPOTENCY" in roles["redis://idempotency:6379/0"][0]
    assert roles["redis://default:6379/0"] == ["cache:default"]
