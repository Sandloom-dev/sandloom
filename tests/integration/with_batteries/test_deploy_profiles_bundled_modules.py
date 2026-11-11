"""The shipped microservices profile against the bundled modules (moved from
the kernel's ``test_deploy_profiles.py``, KB-05c).

These read what identity, tenancy and billing are once installed: their
module names (entry points), the contracts they consume and the claims they
contribute. Each service gets its own database and binds exactly the
contracts its modules consume and do not provide, the token issuer binds
every remote claims contributor with its criticality, every service keys its
responses and pins its providers, boots alone with its bindings, and the
profile passes the split invariants applications are checked with
(SANDLOOM-T014). The profile's environment and helpers are the kernel
suite's.
"""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.descriptors.module_names import module_name
from sandloom.core.tests.test_deploy_profiles import (
    DEPLOY,
    HTTP_SERVICES,
    SHELL,
    _app_processes,
    _broken,
    _env,
    _load,
    _validate,
)

pytestmark = pytest.mark.skipif(
    not DEPLOY.is_dir(), reason="deploy/ profiles not in this checkout"
)


@pytest.mark.parametrize(
    ("edits", "expected"),
    [
        (
            {
                "compose/microservices.yml": (
                    '  SERVICE_BINDINGS: \'{"tenancy.membership.v1"',
                    '  SERVICE_BINDINGS_X: \'{"tenancy.membership.v1"',
                )
            },
            "billing: SERVICE_BINDINGS binds [], its modules need "
            "['tenancy.membership.v1']",
        ),
    ],
    ids=["binding"],
)
def test_the_split_invariants_report_what_breaks_them(
    tmp_path: Path, edits: dict[str, tuple[str, str]], expected: str
) -> None:
    """The binding case of the kernel suite's test of the same name: which
    contracts billing needs is read from the installed module."""
    from sandloom.core.testing.deploy_profile import profile_problems

    problems = profile_problems(_broken(tmp_path, edits)).problems
    assert any(expected in p and "SANDLOOM-T014" in p for p in problems), problems


def test_microservices_db_per_service_and_bindings() -> None:
    services = _load("compose/microservices.yml")["services"]
    http = {n: services[n] for n in HTTP_SERVICES}
    dbs = {_env(s, SHELL)["DATABASE_URL"].rsplit("/", 1)[1] for s in http.values()}
    assert len(dbs) == len(http) and {"sandloom_identity", "sandloom_billing"} <= dbs
    created = services["db"]["environment"]["POSTGRES_MULTIPLE_DATABASES"].split()
    assert set(created) == dbs

    for name, svc in http.items():
        env = _env(svc, SHELL)
        assert env["EVENT_BUS_BACKEND"] == "streams"
        assert ":?" in svc["environment"]["INTERNAL_API_TOKEN"]
        # Its own migrate one-shot, against its own database, gates startup.
        migrate = services[f"migrate-{name}"]
        # It creates its database when missing (a service added to a stack
        # whose volume init-databases.sh already ran on).
        assert migrate["command"] == [
            "sandloom",
            "migrate",
            "upgrade-all",
            "--apply",
            "--create-databases",
        ]
        assert _env(migrate, SHELL)["DATABASE_URL"] == env["DATABASE_URL"]
        assert _env(migrate, SHELL)["SANDLOOM_MODULES"] == env["SANDLOOM_MODULES"]
        assert svc["depends_on"][f"migrate-{name}"] == {
            "condition": "service_completed_successfully"
        }
        # Bindings only point at OTHER services that provide the contract.
        bindings = json.loads(env.get("SERVICE_BINDINGS") or "{}")
        for contract, binding in bindings.items():
            owner = _service_of(services, contract.split(".")[0])
            assert owner != name
            assert binding["base_url"] == f"http://{owner}:2222"

    billing = json.loads(_env(http["billing"], SHELL)["SERVICE_BINDINGS"])
    assert "tenancy.membership.v1" in billing


SRC = Path(__file__).resolve().parents[3] / "src"


#: ``add_claims_contributor(fn, critical=..., fail_safe=...)``: the keyword
#: arguments, whatever their order (billing declares its fail-safe keys).
_CONTRIBUTOR_RE = re.compile(
    r"add_claims_contributor\(\s*\w+\s*((?:,\s*\w+\s*=\s*[\w.]+\s*)*),?\s*\)"
)


def _claims_contributor_criticality(module: str) -> bool | None:
    """``sandloom_tenancy`` -> True/False (critical?) or None (contributes nothing).

    Read statically from the module's ``register()`` source so the test does
    not depend on booting every module.
    """
    init = SRC.joinpath(*module.split("."), "__init__.py")
    matches = _CONTRIBUTOR_RE.findall(init.read_text())
    if not matches:
        return None
    return any("critical=True" in re.sub(r"\s", "", kw) for kw in matches)


def test_token_issuer_binds_every_remote_claims_contributor() -> None:
    """The issuer (the service loading identity) must collect the claims of
    every claims-contributing module deployed in ANOTHER service, with the
    same criticality as the contributor — otherwise tokens minted there
    silently lack tenancy's ``tenant_ids`` / billing's subscription claims."""
    services = _load("compose/microservices.yml")["services"]
    http = {n: services[n] for n in HTTP_SERVICES}
    modules = {
        n: json.loads(_env(s, SHELL)["SANDLOOM_MODULES"]) for n, s in http.items()
    }
    issuers = [n for n, mods in modules.items() if "sandloom_identity" in mods]
    assert issuers == ["identity"]
    issuer = issuers[0]
    bindings = json.loads(_env(http[issuer], SHELL).get("SERVICE_BINDINGS") or "{}")

    contributors = 0
    for name, mods in modules.items():
        if name == issuer:
            continue
        for module in mods:
            critical = _claims_contributor_criticality(module)
            if critical is None:
                continue
            contributors += 1
            short = module_name(module)
            contract = f"{short}.claims.v1"
            assert contract in bindings, (
                f"{issuer} mints tokens but does not bind {contract} "
                f"(deployed in {name})"
            )
            assert bindings[contract]["base_url"] == f"http://{name}:2222"
            bound_critical = bindings[contract].get("critical", True) is not False
            assert bound_critical is critical, (contract, bindings[contract])
    # tenancy (critical) and billing (non-critical) both contribute claims.
    assert contributors >= 2
    assert _claims_contributor_criticality("sandloom_tenancy") is True
    assert _claims_contributor_criticality("sandloom_billing") is False


def _consumed_not_local(modules: list[str]) -> set[str]:
    """Contracts the modules declare in ``consumes`` that they don't provide.

    Loads the modules into a fresh, I/O-free facade (registration only) and
    reads the loader's consumes preflight.
    """
    from fastapi import FastAPI

    from sandloom.core.facade import CoreFacade
    from sandloom.core.loader.plugin_loader import load_module_topology
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    summary = load_module_topology(core, modules)
    return {
        entry["contract"]
        for entries in summary.consumes.values()
        for entry in entries
        if entry["resolution"] != "local"
    }


def _deployed(services: dict[str, Any], contract: str, consumer: str) -> bool:
    """Is the contract's provider deployed in some service? A consumed
    contract whose provider module is not in the profile cannot be bound: that
    is a warning (SANDLOOM-T003), not a topology the feature author must choose."""
    if _service_of(services, contract.split(".")[0]) is not None:
        return True
    warnings.warn(
        f"SANDLOOM-T003: {consumer} consumes {contract}, but its provider module is "
        "not deployed in compose/microservices.yml (the binding is "
        "missing there) — `sandloom add deploy <module> --service <name>` "
        "adds it",
        stacklevel=2,
    )
    return False


def test_microservices_bindings_are_exactly_the_consumed_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every service binds exactly what its modules consume and don't provide
    (+ the issuer's remote ``<module>.claims.v1``) — no missing binding (it
    would fail at startup / first use) and no stale one (dead coupling)."""
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    services = _load("compose/microservices.yml")["services"]
    http = {n: services[n] for n in HTTP_SERVICES}
    modules = {
        n: json.loads(_env(s, SHELL)["SANDLOOM_MODULES"]) for n, s in http.items()
    }
    for name, mods in modules.items():
        bindings = json.loads(_env(http[name], SHELL).get("SERVICE_BINDINGS") or "{}")
        expected = {
            contract
            for contract in _consumed_not_local(mods)
            if _deployed(services, contract, name)
        }
        if "sandloom_identity" in mods:  # the token issuer collects remote claims
            for other, other_mods in modules.items():
                if other == name:
                    continue
                for module in other_mods:
                    if _claims_contributor_criticality(module) is not None:
                        expected.add(f"{module_name(module)}.claims.v1")
        assert set(bindings) == expected, (name, sorted(bindings), sorted(expected))
    assert "tenancy.membership.v1" in _consumed_not_local(["sandloom_billing"])


def test_every_service_keys_its_responses_and_callers_pin_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EVERY microservices process (providers such as tenancy included) holds
    its own X25519 key, and every binding pins its provider's service id — so
    every request authenticates and every response is keyed."""
    from sandloom.core.rpc_binding import own_dh_key

    procs = _app_processes("compose/microservices.yml")
    ids = set()
    for name, svc in procs.items():
        with monkeypatch.context() as mp:
            cfg = _validate(_env(svc, SHELL), mp)
        own = own_dh_key(cfg)
        assert own is not None and own.service_id == cfg.rpc_service_id, name
        ids.add(cfg.rpc_service_id)
        services = _load("compose/microservices.yml")["services"]
        for contract, binding in cfg.service_bindings.items():
            owner = _service_of(services, contract.split(".")[0])
            assert binding.get("service_id") == owner, (name, contract)
    assert ids == set(HTTP_SERVICES)


def test_the_profile_passes_the_split_invariants_apps_are_checked_with() -> None:
    """``sandloom test`` (schema & deploy) checks an application's profile with
    :func:`profile_problems` (SANDLOOM-T014); the framework's own passes it."""
    from sandloom.core.testing.deploy_profile import profile_problems

    report = profile_problems(DEPLOY, served=["sandloom_identity", "sandloom_tenancy"])
    assert report.problems == []
    assert report.warnings == []


def test_every_service_boots_alone_with_its_bindings() -> None:
    """Each HTTP service's SANDLOOM_MODULES, with exactly its own environment,
    boots as ``sandloom doctor`` boots a process (settings, startup preflight,
    composition root, every consumed contract bound; no network)."""
    from sandloom.core.testing.deploy_profile import boot_problems

    assert boot_problems(DEPLOY, SHELL) == {name: [] for name in HTTP_SERVICES}


def _service_of(services: dict[str, Any], short: str) -> str | None:
    """The HTTP service whose SANDLOOM_MODULES include the module ``*.<short>``."""
    for name in HTTP_SERVICES:
        modules = json.loads(_env(services[name], SHELL)["SANDLOOM_MODULES"])
        if any(module_name(m) == short for m in modules):
            return name
    return None
