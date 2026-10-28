"""The shipped compose profiles (deploy/*.yml) must actually start.

Pins D1/D2: every process (migrate, HTTP services, worker, beat) gets an
environment that validates against ``Settings`` in prod; billing never
defaults to the fake PSP; the microservices profile gives each service its own
database + migrate one-shot, wires SERVICE_BINDINGS to the owning service,
requires INTERNAL_API_TOKEN and uses the Streams bus; both profiles run the
framework worker (``sandloom.core.worker``) — not identity's.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import warnings
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.descriptors.module_names import module_name

yaml = pytest.importorskip("yaml")

DEPLOY = Path(__file__).resolve().parents[4] / "deploy"
pytestmark = pytest.mark.skipif(
    not DEPLOY.is_dir(), reason="deploy/ profiles not in this checkout"
)


def _jwt_pair() -> tuple[str, str]:
    from sandloom.core.signing_keys import generate_private_key, private_pem, public_pem

    key = generate_private_key("EdDSA")
    return private_pem(key), public_pem(key)


_JWT_PRIVATE, _JWT_PUBLIC = _jwt_pair()


def _http_services() -> tuple[str, ...]:
    """The microservices profile's HTTP services, discovered (not listed):
    every service with SANDLOOM_MODULES that is not a migrate / worker / beat."""
    path = DEPLOY / "compose/microservices.yml"
    if not path.is_file():
        return ()
    services = yaml.safe_load(path.read_text())["services"]
    return tuple(
        name
        for name, svc in services.items()
        if "SANDLOOM_MODULES" in (svc.get("environment") or {})
        and not name.startswith("migrate-")
        and not name.endswith(("-worker", "-beat"))
    )


HTTP_SERVICES = _http_services()
_RPC = {svc: _jwt_pair() for svc in HTTP_SERVICES}


def _dh_pair() -> tuple[str, str]:
    from sandloom.core.signing_keys import (
        generate_x25519_key,
        x25519_private_pem,
        x25519_public_pem,
    )

    key = generate_x25519_key()
    return x25519_private_pem(key), x25519_public_pem(key)


_DH = {svc: _dh_pair() for svc in HTTP_SERVICES}

#: The callers' Ed25519 identities: they sign COMMAND calls
#: (RPC_COMMANDS_REQUIRE_SIGNATURE, on in prod).
ED25519_SHELL = {
    **{f"{svc.upper()}_RPC_PRIVATE_KEY": _RPC[svc][0] for svc in _RPC},
    **{f"{svc.upper()}_RPC_KEY_ID": f"{svc}-1" for svc in _RPC},
    "RPC_SERVICE_KEYS": json.dumps({svc: {f"{svc}-1": _RPC[svc][1]} for svc in _RPC}),
}

SHELL = {
    **ED25519_SHELL,
    **{f"{svc.upper()}_RPC_DH_PRIVATE_KEY": _DH[svc][0] for svc in _DH},
    **{f"{svc.upper()}_RPC_DH_KEY_ID": f"{svc}-dh-1" for svc in _DH},
    "RPC_SERVICE_DH_KEYS": json.dumps(
        {svc: {f"{svc}-dh-1": _DH[svc][1]} for svc in _DH}
    ),
    "JWT_PRIVATE_KEY": _JWT_PRIVATE,
    "JWT_PUBLIC_KEYS": json.dumps({"jwt-1": _JWT_PUBLIC}),
    "JWT_KID": "jwt-1",
    "JWT_SECRET": secrets.token_hex(32),
    "INTERNAL_API_TOKEN": secrets.token_hex(32),
    "TWOFA_ENCRYPTION_KEY": secrets.token_hex(32),
    "BILLING_PROVIDER": "fake",
    "SANDLOOM_ACCEPTED_RISKS": "local_storage_in_prod,fake_psp_in_prod",
}


def _load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((DEPLOY / name).read_text())
    assert isinstance(data, dict)
    return data


def _interpolate(value: object, env: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        var, op, arg = m.group(1), m.group(2), m.group(3)
        if env.get(var):
            return env[var]
        if op == ":-":
            return arg or ""
        raise AssertionError(f"compose requires {var}: {arg}")

    return re.sub(r"\$\{(\w+)(?:(:[-?])([^}]*))?\}", repl, str(value))


def _env(service: dict[str, Any], shell: dict[str, str]) -> dict[str, str]:
    return {k: _interpolate(v, shell) for k, v in service["environment"].items()}


def _validate(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Any:
    from sandloom.core.config.settings import Settings

    # Issuer-only material a process does NOT receive must not leak in from
    # the test runner's own environment.
    for key in (
        "JWT_SECRET",
        "JWT_PRIVATE_KEY",
        "JWT_PREVIOUS_SECRETS",
        "RPC_PRIVATE_KEY",
        "RPC_KEY_ID",
        "RPC_SERVICE_ID",
        "RPC_DH_PRIVATE_KEY",
        "RPC_DH_KEY_ID",
    ):
        if key not in env:
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    assert cfg.is_prod
    return cfg


@pytest.mark.parametrize(
    "profile", ["compose/monolith.yml", "compose/microservices.yml"]
)
def test_billing_provider_must_be_chosen(profile: str) -> None:
    services = _load(profile)["services"]
    billing_envs = [
        s["environment"]["BILLING_PROVIDER"]
        for s in services.values()
        if "BILLING_PROVIDER" in (s.get("environment") or {})
    ]
    assert billing_envs, "no process passes BILLING_PROVIDER"
    for raw in billing_envs:
        assert ":?" in raw and ":-fake" not in raw
        with pytest.raises(AssertionError, match="BILLING_PROVIDER"):
            _interpolate(raw, {})


@pytest.mark.parametrize(
    "profile", ["compose/monolith.yml", "compose/microservices.yml"]
)
def test_every_process_env_validates(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, svc in _load(profile)["services"].items():
        if "environment" not in svc or "SANDLOOM_MODULES" not in svc["environment"]:
            continue  # gateway / db / redis
        with monkeypatch.context() as mp:
            cfg = _validate(_env(svc, SHELL), mp)
            assert cfg.sandloom_modules, name
            env = svc["environment"]
            assert "SANDLOOM_ACCEPTED_RISKS" in env, name
            if "sandloom_billing" in cfg.sandloom_modules:
                assert cfg.accepts_risk("fake_psp_in_prod"), name
            if "sandloom_identity" in cfg.sandloom_modules:
                # identity refuses to start in prod without a dedicated key.
                assert ":?" in env.get("TWOFA_ENCRYPTION_KEY", ""), name
                assert cfg.twofa_encryption_key, name


@pytest.mark.parametrize(
    "profile", ["compose/monolith.yml", "compose/microservices.yml"]
)
def test_worker_and_beat_use_framework_entrypoint(profile: str) -> None:
    services = _load(profile)["services"]
    commands = {n: s.get("command") for n, s in services.items()}
    workers = [n for n, c in commands.items() if c and "worker" in c and "celery" in c]
    beats = [n for n, c in commands.items() if c and "beat" in c and "celery" in c]
    assert workers and beats
    for name in workers + beats:
        cmd = commands[name]
        assert cmd[:3] == ["celery", "-A", "sandloom.core.worker"], (name, cmd)
    assert not any(
        "sandloom_identity.tasks" in json.dumps(c) for c in commands.values()
    )


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


def _service_of(services: dict[str, Any], short: str) -> str | None:
    """The HTTP service whose SANDLOOM_MODULES include the module ``*.<short>``."""
    for name in HTTP_SERVICES:
        modules = json.loads(_env(services[name], SHELL)["SANDLOOM_MODULES"])
        if any(module_name(m) == short for m in modules):
            return name
    return None


SRC = Path(__file__).resolve().parents[3]
_CONTRIBUTOR_RE = re.compile(
    r"add_claims_contributor\(\s*\w+\s*(?:,\s*critical\s*=\s*(True|False))?\s*\)"
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
    return any(flag == "True" for flag in matches)


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


def test_init_script_is_mounted() -> None:
    services = _load("compose/microservices.yml")["services"]
    mounts = services["db"]["volumes"]
    assert any("init-databases.sh:/docker-entrypoint-initdb.d/" in m for m in mounts)
    assert (DEPLOY / "postgres" / "init-databases.sh").is_file()


# --------------------------------------------------------------------------- #
# Split-review hardening: parity, module requirements, workers, Redis, gateway
# --------------------------------------------------------------------------- #
PROFILES = ["compose/monolith.yml", "compose/microservices.yml"]

#: Settings that must be IDENTICAL in every process of a profile (and present
#: in both profiles): a token minted by one service is verified by all, the
#: revocation marks / bus live in the shared cache, gates share one policy.
PARITY_KEYS = (
    "ENVIRONMENT",
    "SHUTDOWN_BUDGET_SECONDS",
    "EVENT_PAYLOAD_MAX_BYTES",
    "EVENT_STREAM_RETENTION_SECONDS",
    "EVENT_STREAM_MAXLEN",
    "EVENT_STREAM_PREFIX",
    "CACHE_URL",
    "JWT_KID",
    "JWT_ISSUER",
    "JWT_AUDIENCE",
    "ACCESS_TOKEN_EXPIRE_MINUTES",
    "REFRESH_TOKEN_EXPIRE_DAYS",
    "INTERNAL_API_TOKEN",
    "FRONTEND_URL",
    "BILLING_DENY_ON_PAST_DUE",
    "NOTIFIER_BACKEND",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_USE_TLS",
)


#: Per-profile parity: the monolith shares one HS secret; the microservices
#: share the asymmetric verification keys (the private key + JWT_SECRET are
#: identity-only, see test_only_the_issuer_holds_signing_material).
PROFILE_PARITY_KEYS = {
    "compose/monolith.yml": ("JWT_SECRET", "JWT_PREVIOUS_SECRETS"),
    "compose/microservices.yml": (
        "JWT_ALGORITHM",
        "JWT_PUBLIC_KEYS",
        "RPC_SERVICE_KEYS",
        "RPC_ALLOWED_CALLS",
        "RPC_SERVICE_DH_KEYS",
    ),
}


def _app_processes(profile: str) -> dict[str, dict[str, Any]]:
    return {
        name: svc
        for name, svc in _load(profile)["services"].items()
        if "SANDLOOM_MODULES" in (svc.get("environment") or {})
    }


@pytest.mark.parametrize("profile", PROFILES)
def test_security_relevant_settings_identical_across_processes(profile: str) -> None:
    procs = _app_processes(profile)
    assert len(procs) >= 4
    for key in (*PARITY_KEYS, *PROFILE_PARITY_KEYS[profile]):
        values = {name: svc["environment"].get(key) for name, svc in procs.items()}
        missing = sorted(n for n, v in values.items() if v is None)
        assert not missing, f"{key} missing in {missing} ({profile})"
        assert len(set(values.values())) == 1, f"{key} differs: {values}"


def test_parity_keys_default_identically_in_both_profiles() -> None:
    mono = next(iter(_app_processes(PROFILES[0]).values()))["environment"]
    split = next(iter(_app_processes(PROFILES[1]).values()))["environment"]
    for key in PARITY_KEYS:
        if key in ("INTERNAL_API_TOKEN", "JWT_KID"):
            # optional in the monolith, mandatory between services
            continue
        assert _interpolate(mono[key], SHELL) == _interpolate(split[key], SHELL), key


@pytest.mark.parametrize("profile", PROFILES)
def test_listed_modules_are_required_in_every_process(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo / import failure of a listed module must abort the process, not
    leave a healthy service with no routes (MODULE_REQUIRED semantics)."""
    from sandloom.core.config.settings import settings
    from sandloom.core.loader import plugin_loader

    for name, svc in _app_processes(profile).items():
        with monkeypatch.context() as mp:
            cfg = _validate(_env(svc, SHELL), mp)
            mp.setattr(settings, "sandloom_modules", list(cfg.sandloom_modules))
            mp.setattr(settings, "module_required", list(cfg.module_required))
            mp.setattr(plugin_loader, "_defaulted_modules", None)
            plugin_loader.apply_module_defaults(["sandloom.unused"], [])
            assert set(settings.module_required) == set(cfg.sandloom_modules), name


@pytest.mark.parametrize("profile", PROFILES)
def test_workers_are_sized_and_health_checked(profile: str) -> None:
    services = _load(profile)["services"]
    workers = {
        n: s
        for n, s in services.items()
        if s.get("command")
        and s["command"][:4] == ["celery", "-A", "sandloom.core.worker", "worker"]
    }
    assert workers
    for name, svc in workers.items():
        env = _env(svc, SHELL)
        assert "WORKER_CONCURRENCY" in env, name
        assert int(env["DB_POOL_SIZE"]) <= 2 and int(env["DB_MAX_OVERFLOW"]) <= 2, name
        hc = svc["healthcheck"]
        assert not hc.get("disable"), name
        assert hc["test"] == [
            "CMD",
            "python",
            "-m",
            "sandloom.core.worker",
            "healthcheck",
        ]
        limit = svc["deploy"]["resources"]["limits"]["memory"]
        assert limit.endswith("m") and int(limit[:-1]) >= 512, (name, limit)
        per_child_kib = int(env["WORKER_MAX_MEMORY_PER_CHILD"])
        from sandloom.core.worker import MAX_DEFAULT_WORKER_CONCURRENCY

        # Children at their recycle ceiling still fit beside the main process.
        assert per_child_kib * MAX_DEFAULT_WORKER_CONCURRENCY < int(limit[:-1]) * 1024


def _modules_with_periodic_tasks() -> set[str]:
    """Bundled modules whose ``tasks.py`` defines the beat hook (AST, no import)."""
    import ast

    from sandloom.core.worker import PERIODIC_HOOK

    src = Path(__file__).resolve().parents[3]  # .../src (top-level modules)
    found: set[str] = set()
    for tasks in src.glob("*/tasks.py"):
        tree = ast.parse(tasks.read_text())
        if any(
            isinstance(node, ast.FunctionDef) and node.name == PERIODIC_HOOK
            for node in tree.body
        ):
            found.add(tasks.parent.name)
    return found


def test_periodic_task_modules_are_discovered() -> None:
    # Guards the scan below: billing (intent reconciler) and identity
    # (token cleanup) both ship beat schedules.
    assert {"sandloom_billing", "sandloom_identity"} <= _modules_with_periodic_tasks()


@pytest.mark.parametrize("profile", PROFILES)
def test_every_periodic_task_set_has_exactly_one_beat(profile: str) -> None:
    """Every module with ``setup_periodic_tasks`` that a profile serves has
    exactly ONE beat loading it, a worker loading it on that beat's broker, and
    no two beats share a broker — so no schedule is missing or runs twice.
    Also a step of ``sandloom test`` (``schema & deploy``, SANDLOOM-T010)."""
    from sandloom.core.testing.static_checks import periodic_beat_problems

    problems = periodic_beat_problems(
        DEPLOY / profile, periodic=sorted(_modules_with_periodic_tasks())
    )
    assert not problems, "\n".join(problems)


def test_a_module_with_periodic_tasks_and_no_beat_is_reported(tmp_path: Path) -> None:
    from sandloom.core.testing.static_checks import periodic_beat_problems

    profile = tmp_path / "compose/microservices.yml"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "tenancy": {
                        "environment": {"SANDLOOM_MODULES": '["sandloom_tenancy"]'},
                    }
                }
            }
        )
    )
    (problem,) = periodic_beat_problems(profile, periodic=["sandloom_tenancy"])
    assert "SANDLOOM-T010" in problem and "exactly one beat, got none" in problem


@pytest.mark.parametrize("profile", PROFILES)
def test_redis_bus_is_persistent(profile: str) -> None:
    redis = _load(profile)["services"]["redis"]
    cmd = redis["command"]
    assert cmd[cmd.index("--appendonly") + 1] == "yes"
    assert cmd[cmd.index("--maxmemory-policy") + 1] == "noeviction"
    assert any(str(v).endswith(":/data") for v in redis["volumes"])


@pytest.mark.parametrize("profile", PROFILES)
def test_idempotency_cache_opt_in_is_shared_and_durable(profile: str) -> None:
    """IDEMPOTENCY_CACHE_INSTANCE is off by default; once enabled it (with
    INFRA / SECURITY_CACHE_INSTANCE) is identical in every process, and its
    Redis fsyncs every write and never evicts, like a security Redis."""
    procs = _app_processes(profile)
    for key in ("IDEMPOTENCY_CACHE_INSTANCE", "SECURITY_CACHE_INSTANCE", "INFRA"):
        values = {name: svc["environment"].get(key) for name, svc in procs.items()}
        assert len(set(values.values())) == 1, f"{key} differs: {values}"
    services = _load(profile)["services"]
    if "idempotency-redis" in services:
        cmd = services["idempotency-redis"]["command"]
        assert cmd[cmd.index("--appendfsync") + 1] == "always"
        assert cmd[cmd.index("--maxmemory-policy") + 1] == "noeviction"
    else:
        text = (DEPLOY / profile).read_text()
        assert "# idempotency-redis:" in text
        assert '"--appendfsync", "always"' in text


@pytest.mark.parametrize("profile", PROFILES)
def test_one_named_image_for_every_built_service(profile: str) -> None:
    built = {n: s for n, s in _load(profile)["services"].items() if "build" in s}
    images = {s.get("image") for s in built.values()}
    assert len(images) == 1 and None not in images, images


def test_microservices_network_is_parametrized() -> None:
    data = _load("compose/microservices.yml")
    subnet = data["networks"]["default"]["ipam"]["config"][0]["subnet"]
    gateway = data["services"]["gateway"]
    assert "${SANDLOOM_SUBNET" in subnet
    assert "${SANDLOOM_GATEWAY_IP" in gateway["networks"]["default"]["ipv4_address"]
    assert any("${GATEWAY_PORT" in p for p in gateway["ports"])
    trusted = data["services"]["identity"]["environment"]["TRUSTED_PROXY_IPS"]
    assert "${SANDLOOM_GATEWAY_IP" in trusted


_DEFAULT = re.compile(r"\$\{(\w+):-([^}]*)\}")


def _defaults(value: str) -> tuple[set[str], str]:
    """`${VAR:-default}` placeholders in a compose value -> (vars, value with
    every placeholder replaced by its default)."""
    names = {m.group(1) for m in _DEFAULT.finditer(value)}
    return names, _DEFAULT.sub(lambda m: m.group(2), value)


def test_microservices_static_addresses_outside_dynamic_pool() -> None:
    """Every static `ipv4_address` lies in the subnet but OUTSIDE `ip_range`.

    Without an `ip_range` Docker hands dynamic addresses from .2 upward, so
    with enough containers starting before the gateway one of them takes the
    gateway's fixed .10 and the gateway fails with "Address already in use".
    The defaults of the variables must agree, and the trusted proxy hop must
    be the gateway's address.
    """
    data = _load("compose/microservices.yml")
    ipam = data["networks"]["default"]["ipam"]["config"]
    assert len(ipam) == 1
    subnet_vars, subnet_default = _defaults(ipam[0]["subnet"])
    assert "ip_range" in ipam[0], "dynamic pool must exclude static addresses"
    range_vars, range_default = _defaults(ipam[0]["ip_range"])
    assert subnet_vars == {"SANDLOOM_SUBNET"} and range_vars == {"SANDLOOM_IP_RANGE"}
    subnet = ipaddress.ip_network(subnet_default)
    pool = ipaddress.ip_network(range_default)
    assert pool.subnet_of(subnet)  # type: ignore[arg-type]

    static: dict[str, tuple[set[str], str]] = {}
    for name, svc in data["services"].items():
        nets = svc.get("networks")
        if isinstance(nets, dict):
            for cfg in nets.values():
                if isinstance(cfg, dict) and "ipv4_address" in cfg:
                    static[name] = _defaults(str(cfg["ipv4_address"]))
    assert "gateway" in static
    for name, (_, addr_default) in static.items():
        addr = ipaddress.ip_address(addr_default)
        assert addr in subnet, f"{name}: {addr} not in subnet {subnet}"
        assert addr not in pool, f"{name}: {addr} inside dynamic ip_range {pool}"
        assert addr not in (subnet.network_address, subnet.network_address + 1)
    gw_vars, gw_default = static["gateway"]
    assert gw_vars == {"SANDLOOM_GATEWAY_IP"}

    for name, svc in data["services"].items():
        env = svc.get("environment") or {}
        if isinstance(env, dict) and "TRUSTED_PROXY_IPS" in env:
            t_vars, t_default = _defaults(str(env["TRUSTED_PROXY_IPS"]))
            assert t_vars == {"SANDLOOM_GATEWAY_IP"}, name
            assert json.loads(t_default) == [f"{gw_default}/32"], name


# ---- gateway ----------------------------------------------------------------
NGINX = DEPLOY / "gateway" / "nginx.conf"
_LOCATION_RE = re.compile(
    r"location\s+(=\s+)?(\S+)\s*\{[^}]*?(?:proxy_pass\s+http://(\w+);|return\s+(\d+))"
)
INTERNAL_PATHS = (
    "/_sandloom/rpc/tenancy.membership.v1",
    "/diagnostics/core",
    "/metrics",
    "/contracts",
    "/ready",
    "/health/simple",
    "/docs",
    "/openapi.json",
)


def _locations() -> list[tuple[bool, str, str | None, str | None]]:
    text = NGINX.read_text()
    return [
        (bool(m.group(1)), m.group(2), m.group(3), m.group(4))
        for m in _LOCATION_RE.finditer(text)
    ]


def _route(path: str) -> tuple[str | None, str | None]:
    """nginx prefix matching: exact first, then the longest prefix."""
    locs = _locations()
    for exact, loc, upstream, ret in locs:
        if exact and loc == path:
            return upstream, ret
    best = max(
        (item for item in locs if not item[0] and path.startswith(item[1])),
        key=lambda item: len(item[1]),
        default=None,
    )
    return (best[2], best[3]) if best else (None, None)


def _module_route_prefixes(module: str) -> set[str]:
    from fastapi import FastAPI

    from sandloom.core.facade import CoreFacade
    from sandloom.core.loader.plugin_loader import load_module_topology
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    core = CoreFacade(app=FastAPI())
    core.register_schema_registry(InMemorySchemaRegistry())
    summary = load_module_topology(core, [module])
    assert summary.success(), summary.to_dict()
    return {getattr(r, "path", "") for r in core.app.router.routes} - {""}


def test_gateway_routes_every_module_path_to_its_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "module_load_entrypoints", False)
    services = _load("compose/microservices.yml")["services"]
    owners = {
        module: name
        for name in HTTP_SERVICES
        for module in json.loads(_env(services[name], SHELL)["SANDLOOM_MODULES"])
    }
    for module, service in owners.items():
        for path in _module_route_prefixes(module):
            if path.startswith(("/docs", "/openapi", "/redoc")):
                continue
            concrete = re.sub(r"\{[^}]+\}", "x", path)
            upstream, _ = _route(concrete)
            assert upstream == service, f"{path} ({module}) -> {upstream}"
            assert _route(concrete.rstrip("/") or "/")[0] == service, path


def test_gateway_never_exposes_internal_surfaces() -> None:
    for path in INTERNAL_PATHS:
        upstream, ret = _route(path)
        assert upstream is None and ret == "404", (path, upstream, ret)
    assert _route("/health") == (None, "200")  # the gateway's own probe


def test_gateway_follows_dns_and_fails_fast() -> None:
    text = NGINX.read_text()
    assert re.search(r"^\s*resolver\s+127\.0\.0\.11\b.*valid=", text, re.M)
    servers = re.findall(r"^\s*server\s+([\w.-]+:\d+)([^;]*);", text, re.M)
    assert servers and all("resolve" in opts for _, opts in servers), servers
    assert text.count("zone ") >= len(servers)
    connect = re.search(r"proxy_connect_timeout\s+(\d+)s;", text)
    assert connect and int(connect.group(1)) <= 5
    image = _load("compose/microservices.yml")["services"]["gateway"]["image"]
    major, minor = (int(x) for x in image.split(":")[1].split("-")[0].split(".")[:2])
    assert (major, minor) >= (1, 28) or image.startswith("nginx:1.27.")  # resolve


def test_gateway_body_limit_matches_app_upload_limit() -> None:
    from sandloom.core.config.settings import Settings

    match = re.search(r"client_max_body_size\s+(\d+)m;", NGINX.read_text())
    assert match
    default = Settings.model_fields["max_upload_size_bytes"].default
    assert int(match.group(1)) * 1024 * 1024 == default


@pytest.mark.parametrize("profile", PROFILES)
def test_every_long_running_process_names_its_service(profile: str) -> None:
    """``SANDLOOM_SERVICE_NAME`` labels consumer ids / inventory / diagnostics."""
    names: dict[str, str] = {}
    for name, svc in _app_processes(profile).items():
        if svc.get("restart") == "no":
            continue  # migrate one-shots inherit their service's name
        value = _env(svc, SHELL).get("SANDLOOM_SERVICE_NAME", "")
        assert value, f"{name} has no SANDLOOM_SERVICE_NAME ({profile})"
        names[name] = value
    assert len(set(names.values())) == len(names), names


def _max_connections(profile: str) -> int:
    db = _load(profile)["services"]["db"]
    command = db.get("command") or []
    joined = " ".join(command) if isinstance(command, list) else str(command)
    match = re.search(r"max_connections=(\d+)", joined)
    assert match, f"{profile}: set Postgres max_connections explicitly"
    return int(match.group(1))


#: Headroom kept free for psql / admin / monitoring sessions.
ADMIN_CONNECTION_RESERVE = 10
#: sandloom-migrate uses NullPool: one connection (plus a short-lived admin one).
MIGRATE_CONNECTIONS = 2


def _process_count(svc: dict[str, Any], env: dict[str, str]) -> int:
    from sandloom.core.worker import MAX_DEFAULT_WORKER_CONCURRENCY

    command = svc.get("command") or []
    if command[:4] == ["celery", "-A", "sandloom.core.worker", "worker"]:
        raw = env.get("WORKER_CONCURRENCY", "")
        return int(raw) if raw else MAX_DEFAULT_WORKER_CONCURRENCY
    if "beat" in command:
        return 1
    return int(env.get("WEB_CONCURRENCY", "") or 4)  # image default cap: 4


@pytest.mark.parametrize("profile", PROFILES)
def test_connection_budget_fits_max_connections(profile: str) -> None:
    """Every pool of every process at its maximum fits the server's limit.

    Per process: ``DB_POOL_SIZE + DB_MAX_OVERFLOW`` (request pool) plus
    ``CONTRACT_DB_POOL_SIZE + CONTRACT_DB_MAX_OVERFLOW`` (the dedicated
    contract pool); x processes per container (gunicorn workers / prefork
    children); + the migrate one-shots; + an admin reserve.
    """
    from sandloom.core.config.settings import Settings

    defaults = Settings.model_fields
    budget = 0
    rows: dict[str, int] = {}
    for name, svc in _app_processes(profile).items():
        env = _env(svc, SHELL)
        if svc.get("restart") == "no":
            rows[name] = MIGRATE_CONNECTIONS
            budget += MIGRATE_CONNECTIONS
            continue

        def setting(key: str, field: str, env: dict[str, str] = env) -> int:
            raw = env.get(key, "")
            return int(raw) if raw else int(defaults[field].default)

        per_process = (
            setting("DB_POOL_SIZE", "db_pool_size")
            + setting("DB_MAX_OVERFLOW", "db_max_overflow")
            + setting("CONTRACT_DB_POOL_SIZE", "contract_db_pool_size")
            + setting("CONTRACT_DB_MAX_OVERFLOW", "contract_db_max_overflow")
        )
        rows[name] = per_process * _process_count(svc, env)
        budget += rows[name]
    limit = _max_connections(profile)
    assert budget + ADMIN_CONNECTION_RESERVE <= limit, (budget, limit, rows)


def test_microservices_contract_pool_is_small() -> None:
    procs = _app_processes("compose/microservices.yml")
    for name in HTTP_SERVICES:
        env = _env(procs[name], SHELL)
        assert env["CONTRACT_DB_POOL_SIZE"] == "1", name
        assert env["CONTRACT_DB_MAX_OVERFLOW"] == "2", name


def test_only_the_issuer_holds_signing_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Microservices: asymmetric tokens; identity alone can mint.

    Every process validates and passes the kernel's startup role check
    (``token_signing_problem``): identity processes hold JWT_PRIVATE_KEY (and
    JWT_SECRET for their emailed tokens), every other process only the public
    keys — so a compromised tenancy/billing cannot mint a token.
    """
    from sandloom.core.auth.tokens import token_signing_problem

    for name, svc in _app_processes("compose/microservices.yml").items():
        env = svc["environment"]
        issuer = "sandloom_identity" in env["SANDLOOM_MODULES"]
        assert ("JWT_PRIVATE_KEY" in env) is issuer, name
        assert ("JWT_SECRET" in env) is issuer, name
        assert "JWT_PREVIOUS_SECRETS" not in env, name
        with monkeypatch.context() as mp:
            for key in ("JWT_PRIVATE_KEY", "JWT_SECRET"):
                mp.delenv(key, raising=False)
            cfg = _validate(_env(svc, SHELL), mp)
            assert cfg.jwt_asymmetric, name
            assert token_signing_problem(issuer_loaded=issuer, cfg=cfg) is None, name


def test_monolith_shared_secret_passes_the_role_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.auth.tokens import token_signing_problem

    for name, svc in _app_processes("compose/monolith.yml").items():
        with monkeypatch.context() as mp:
            cfg = _validate(_env(svc, SHELL), mp)
            issuer = "sandloom_identity" in cfg.sandloom_modules
            assert token_signing_problem(issuer_loaded=issuer, cfg=cfg) is None, name


def test_rpc_allowlist_is_derived_from_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Microservices: every call is pair-MAC authenticated, every caller
    derives a key for each binding and signs its commands, and the shared
    allowlist is EXACTLY each service's SERVICE_BINDINGS (deny by default:
    nothing more, and every wired call allowed)."""
    from sandloom.core.rpc_identity import allowlist_from_bindings, signing_identity
    from sandloom.core.rpc_pair_mac import caller_key, commands_require_signature

    bindings: dict[str, dict[str, Any]] = {}
    allowed: Any = None
    for name, svc in _app_processes("compose/microservices.yml").items():
        with monkeypatch.context() as mp:
            cfg = _validate(_env(svc, SHELL), mp)
        # MAC for every call; Ed25519 only to sign commands (KCI mitigation)
        assert commands_require_signature(cfg), name
        assert (signing_identity(cfg) is not None) is bool(cfg.service_bindings), name
        rpc_id = cfg.rpc_service_id
        assert rpc_id in HTTP_SERVICES, name
        bindings.setdefault(rpc_id, dict(cfg.service_bindings))
        assert bindings[rpc_id] == cfg.service_bindings, name
        for binding in cfg.service_bindings.values():
            key = caller_key(cfg, str(binding["service_id"]))
            assert (key.caller, key.provider) == (rpc_id, binding["service_id"])
        allowed = cfg.rpc_allowed_calls
    assert allowed == allowlist_from_bindings(bindings)


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


def test_a_module_missing_from_the_microservices_profile_is_a_warning() -> None:
    """R2.6: a module the platform loads but the microservices profile does
    not deploy is reported (SANDLOOM-T003), never a forced topology choice for the
    feature author; `sandloom add deploy` writes service, gateway and bindings."""
    from sandloom.core.descriptors.module_names import installed_modules

    DEFAULT_MODULES = [m.path for m in installed_modules()]

    services = _load("compose/microservices.yml")["services"]
    deployed = {
        module
        for name in HTTP_SERVICES
        for module in json.loads(_env(services[name], SHELL)["SANDLOOM_MODULES"])
    }
    for module in sorted(set(DEFAULT_MODULES) - deployed):
        warnings.warn(
            f"SANDLOOM-T003: {module} is not deployed in the microservices profile "
            "(unreachable there) — `sandloom add deploy "
            f"{module_name(module)} --service <name>` adds it",
            stacklevel=1,
        )
    assert {"sandloom_identity", "sandloom_tenancy", "sandloom_billing"} <= deployed


# ---- the split invariants an application's profile is checked with ---------


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


def _broken(tmp_path: Path, edits: dict[str, tuple[str, str]]) -> Path:
    import shutil

    copy = tmp_path / "deploy"
    shutil.copytree(DEPLOY, copy)
    for rel, (old, new) in edits.items():
        path = copy / rel
        text = path.read_text()
        assert old in text, (rel, old)
        path.write_text(text.replace(old, new, 1))
    return copy


_TENANCY_ID = "  RPC_SERVICE_ID: tenancy\n"


@pytest.mark.parametrize(
    ("edits", "expected"),
    [
        (
            {
                "compose/microservices.yml": (
                    _TENANCY_ID,
                    _TENANCY_ID + "  CACHE_URL: redis://redis:6379/9\n",
                )
            },
            "CACHE_URL differs between processes",
        ),
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
        (
            {
                "gateway/nginx.conf": (
                    "        location / { return 404; }",
                    "        location / { proxy_pass http://identity; }",
                )
            },
            "the gateway exposes /_sandloom/rpc",
        ),
        (
            {
                "compose/microservices.yml": (
                    _TENANCY_ID,
                    _TENANCY_ID + "  JWT_SECRET: x\n",
                )
            },
            "tenancy: JWT_SECRET belongs to the token issuer's processes only",
        ),
        (
            {
                "compose/microservices.yml": (
                    "      migrate-tenancy:\n"
                    "        condition: service_completed_successfully",
                    "      migrate-tenancy:\n        condition: service_started",
                )
            },
            "tenancy: does not wait for migrate-tenancy to complete",
        ),
    ],
    ids=["cache-url", "binding", "internal-route", "issuer-only", "migrate"],
)
def test_the_split_invariants_report_what_breaks_them(
    tmp_path: Path, edits: dict[str, tuple[str, str]], expected: str
) -> None:
    from sandloom.core.testing.deploy_profile import profile_problems

    problems = profile_problems(_broken(tmp_path, edits)).problems
    assert any(expected in p and "SANDLOOM-T014" in p for p in problems), problems


def test_sandloom_test_checks_the_split_profile_of_a_project(tmp_path: Path) -> None:
    """``schema & deploy`` runs the split invariants on the project's own
    profile (an application's ``deploy/``, the framework's ``../deploy``)."""
    from sandloom.core.composition import load_composition_core
    from sandloom.core.testing.static_checks import all_problems

    _broken(
        tmp_path,
        {
            "compose/microservices.yml": (
                _TENANCY_ID,
                _TENANCY_ID + "  JWT_SECRET: x\n",
            )
        },
    )
    problems = all_problems(tmp_path, load_composition_core())
    assert any("SANDLOOM-T014" in p for p in problems), problems
