"""The public split harness derives pinned RPC wiring and issuer-only secrets."""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import jwt
import pytest

from sandloom.core.config.settings import Settings
from sandloom.core.descriptors import ModuleDescriptor
from sandloom.core.rpc_binding import pair_info, pair_key, provider_key
from sandloom.sdk import testing


@pytest.fixture
def topology(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    for name, contract in (
        ("split_issuer", "split_issuer.read.v1"),
        ("split_store", "split_store.read.v1"),
    ):
        mod = types.ModuleType(name)

        def register(
            core: Any, name: str = name, contract: str = contract
        ) -> ModuleDescriptor:
            core.register_service(contract, object())
            return ModuleDescriptor(name=name, version="1")

        mod.register = register  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, mod)
    return {"api": ["split_issuer"], "store": ["split_store"]}


URLS = {"api": "http://127.0.0.1:18001", "store": "http://127.0.0.1:18002"}


def test_split_keys_bindings_and_token_verification(
    topology: dict[str, list[str]],
) -> None:
    env = testing.split_env(topology, URLS, issuer="api")
    assert json.loads(env["api"]["SANDLOOM_MODULES"]) == ["split_issuer"]
    assert json.loads(env["api"]["SERVICE_BINDINGS"]) == {
        "split_store.read.v1": {"base_url": URLS["store"], "service_id": "store"}
    }
    assert json.loads(env["api"]["RPC_ALLOWED_CALLS"]) == {
        "api": ["split_store.read.v1"],
        "store": ["split_issuer.read.v1"],
    }
    assert env["store"]["JWT_PRIVATE_KEY"] == env["store"]["JWT_SECRET"] == ""
    token = jwt.encode(
        {"sub": "user"}, env["api"]["JWT_PRIVATE_KEY"], algorithm="EdDSA"
    )
    public = json.loads(env["store"]["JWT_PUBLIC_KEYS"])[env["api"]["JWT_KID"]]
    assert jwt.decode(token, public, algorithms=["EdDSA"])["sub"] == "user"
    configs = {
        name: Settings(
            _env_file=None,
            DATABASE_URL="sqlite+aiosqlite://",
            CACHE_URL="redis://localhost/0",
            BROKER_URL="redis://localhost/1",
            **{
                key: json.loads(value)
                if key
                in {"SANDLOOM_MODULES", "SERVICE_BINDINGS", "JWT_PREVIOUS_SECRETS"}
                else value
                for key, value in values.items()
            },
        )
        for name, values in env.items()
    }
    bound = provider_key(configs["store"], ("api", "k1"))
    assert bound is not None
    public_dh = json.loads(env["api"]["RPC_SERVICE_DH_KEYS"])["store"]["k1"]
    assert (
        pair_key(
            configs["api"].rpc_dh_private_key,
            public_dh,
            pair_info("api", "k1", "store", "k1"),
        )
        == bound.key
    )


def test_explicit_bindings_are_exact_and_keep_metadata(
    topology: dict[str, list[str]],
) -> None:
    selected = {"api": {"split_store.read.v1": {"critical": False}}, "store": {}}
    env = testing.split_env(topology, URLS, issuer="api", bindings=selected)
    assert (
        json.loads(env["api"]["SERVICE_BINDINGS"])["split_store.read.v1"]["critical"]
        is False
    )
    assert json.loads(env["store"]["SERVICE_BINDINGS"]) == {}
    assert json.loads(env["api"]["RPC_ALLOWED_CALLS"])["store"] == []
    assert selected["api"]["split_store.read.v1"] == {"critical": False}


def test_single_process_and_independent_runs(topology: dict[str, list[str]]) -> None:
    processes = {"api": [*topology["api"], *topology["store"]]}
    one = testing.split_env(processes, {"api": URLS["api"]}, issuer="api")["api"]
    two = testing.split_env(processes, {"api": URLS["api"]}, issuer="api")["api"]
    assert json.loads(one["SERVICE_BINDINGS"]) == {}
    assert one["JWT_PRIVATE_KEY"] != two["JWT_PRIVATE_KEY"]
    assert one["RPC_PRIVATE_KEY"] != two["RPC_PRIVATE_KEY"]


@pytest.mark.parametrize(
    "case", ["issuer", "url", "duplicate", "unknown", "local", "pin", "load", "service"]
)
def test_invalid_topologies_fail_before_wiring(
    topology: dict[str, list[str]], case: str
) -> None:
    urls = dict(URLS)
    issuer = "api"
    bindings = None
    if case == "issuer":
        issuer = "missing"
    elif case == "url":
        urls["api"] = "not-an-http-url"
    elif case == "duplicate":
        topology["store"].append("split_issuer")
    elif case == "unknown":
        bindings = {"api": {"missing.read.v1": {}}}
    elif case == "local":
        bindings = {"api": {"split_issuer.read.v1": {}}}
    elif case == "pin":
        bindings = {"api": {"split_store.read.v1": {"service_id": "wrong"}}}
    elif case == "service":
        topology["bad service"] = topology.pop("store")
        urls["bad service"] = urls.pop("store")
    else:
        topology["api"] = ["no_such_split_module"]
    with pytest.raises(ValueError):
        testing.split_env(topology, urls, issuer=issuer, bindings=bindings)
