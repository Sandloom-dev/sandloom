"""p5_scale_switch — split the Academy app into microservices (app mode).

The app the agent split is p1's product (``<app>/modules/<name>`` plus the
bundled identity, tenancy and billing). The framework mode of p5 (a
kernel-contributor task: the repository's own profile) is scored by the
repository's deploy-profile tests, so every check here skips there.

What the framework's own deploy-profile test pins for its profile, checked on
the app's ``deploy/compose/microservices.yml``:

* the split itself: the services the task names, each module in exactly one
  HTTP service, the gateway routing each product prefix to its service;
* config only: no ``.py`` of the app changed;
* the boot: each HTTP service's ``SANDLOOM_MODULES`` loads alone in the app's own
  interpreter (``_boot_services.py``: registration only, no I/O);
* a database and a migrate one-shot per service; ``SERVICE_BINDINGS`` exactly
  the contracts each service consumes from another one (+ the token
  issuer's remote claims); one beat for the periodic reminders; a durable
  cross-process event bus.

The product's hidden acceptance tests and footgun probes run in-process
(the monolith), as in framework mode: the code did not change.
"""

from __future__ import annotations

import json
import re
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from _dx import app_mode, changed_files, deploy_dir, root_package, worktree

if not app_mode():
    pytest.skip(
        "framework mode: the repository's deploy-profile tests score it",
        allow_module_level=True,
    )

PROFILE = deploy_dir() / "compose/microservices.yml"
SPLIT = {
    "academy": {"catalog", "enrollment"},
    "learning": {"progress", "certificates", "reminders"},
    "insights": {"reporting"},
}
BUNDLED = ("identity", "tenancy", "billing")
PRODUCT = sorted(set().union(*SPLIT.values()))
#: Bundled modules that contribute token claims (the issuer binds their
#: ``<m>.claims.v1`` when they run in another service).
CLAIMS = ("tenancy", "billing")


def _interpolate(value: object) -> str:
    """``${VAR:-default}`` -> default, ``${VAR}`` / ``${VAR:?msg}`` -> ""."""

    def repl(m: re.Match[str]) -> str:
        return m.group(3) or "" if m.group(2) == ":-" else ""

    return re.sub(r"\$\{(\w+)(?:(:[-?])([^}]*))?\}", repl, str(value))


@cache
def _services() -> dict[str, Any]:
    if not PROFILE.is_file():
        return {}
    data = yaml.safe_load(PROFILE.read_text())
    services = data.get("services") if isinstance(data, dict) else None
    return services if isinstance(services, dict) else {}


def _env(svc: Any) -> dict[str, str]:
    raw = svc.get("environment") if isinstance(svc, dict) else None
    if isinstance(raw, list):
        raw = dict(item.split("=", 1) for item in raw if "=" in str(item))
    return {str(k): _interpolate(v) for k, v in (raw or {}).items()}


def _command(svc: Any) -> str:
    cmd = svc.get("command", "") if isinstance(svc, dict) else ""
    return " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)


def _modules(svc: Any) -> list[str]:
    raw = _env(svc).get("SANDLOOM_MODULES", "").strip()
    if not raw:
        return []
    try:
        listed = json.loads(raw)
    except ValueError:
        listed = raw.split(",")
    return [str(m).strip() for m in listed if str(m).strip()]


def _short(module: str) -> str:
    return module.rsplit(".", 1)[-1]


def _http() -> dict[str, Any]:
    """Services that serve HTTP for a module set (not a worker, beat or
    migrate one-shot)."""
    return {
        name: svc
        for name, svc in _services().items()
        if _modules(svc)
        and "celery" not in _command(svc)
        and "migrate" not in _command(svc)
    }


def _hosts() -> dict[str, list[str]]:
    """Short module name -> the HTTP services that load it."""
    out: dict[str, list[str]] = {}
    for name, svc in _http().items():
        for module in _modules(svc):
            out.setdefault(_short(module), []).append(name)
    return out


@cache
def _boot() -> dict[str, Any]:
    """Each HTTP service's modules loaded alone in the app's interpreter."""
    app = worktree()
    python = app / ".venv" / "bin" / "python"
    script = Path(__file__).with_name("_boot_services.py")
    payload = {name: _modules(svc) for name, svc in _http().items()}
    if not python.is_file() or not payload:
        return {}
    done = subprocess.run(
        [str(python), str(script)],
        input=json.dumps(payload),
        cwd=app,
        capture_output=True,
        text=True,
        timeout=600,
    )
    try:
        return json.loads(done.stdout)
    except ValueError:
        return {"_error": done.stderr[-2000:]}


def _bindings(svc: Any) -> dict[str, Any]:
    raw = _env(svc).get("SERVICE_BINDINGS", "").strip() or "{}"
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# --- the split --------------------------------------------------------------


def test_fn_profile_exists() -> None:
    assert _services(), f"no services in {PROFILE}"


def test_fn_services_split_as_asked() -> None:
    http = _http()
    for name, wanted in SPLIT.items():
        assert name in http, f"no HTTP service {name!r}: {sorted(http)}"
        assert {_short(m) for m in _modules(http[name])} == wanted, name


def test_fn_every_module_in_exactly_one_service() -> None:
    hosts = _hosts()
    for short in (*BUNDLED, *PRODUCT):
        assert len(hosts.get(short, [])) == 1, (short, hosts.get(short))
    for short in BUNDLED:
        assert not set(hosts[short]) & set(SPLIT), (short, hosts[short])


def _gateway_text() -> str:
    deploy = deploy_dir()
    files = [
        p
        for p in deploy.rglob("*")
        if p.is_file()
        and (p.suffix in {".conf", ".template"} or p.name.startswith("Caddyfile"))
    ]
    return "\n".join(p.read_text(errors="replace") for p in files)


def _routed_to(prefix: str, text: str) -> set[str]:
    """Upstream hosts a gateway sends ``prefix`` to (nginx or Caddy)."""
    found: set[str] = set()
    for m in re.finditer(
        r"location\s+(?:[=~^]+\s*)?(\S+)\s*\{([^}]*)\}", text, re.S
    ):  # nginx
        if m.group(1).rstrip("/").startswith(prefix):
            found |= set(re.findall(r"proxy_pass\s+https?://([\w.-]+)", m.group(2)))
    upstreams = dict(
        re.findall(r"upstream\s+([\w.-]+)\s*\{[^}]*?server\s+([\w.-]+)", text, re.S)
    )
    found = {upstreams.get(h, h) for h in found}
    for m in re.finditer(
        r"handle(?:_path)?\s+(\S+)\s*\{[^}]*?reverse_proxy\s+([\w.-]+)", text, re.S
    ):  # Caddy
        if m.group(1).rstrip("*/").startswith(prefix):
            found.add(m.group(2))
    return {h.split(":")[0] for h in found}


def test_fn_gateway_routes_each_product_module() -> None:
    text = _gateway_text()
    hosts = _hosts()
    for short in PRODUCT:
        routed = _routed_to(f"/api/v1/{short}", text)
        assert set(hosts.get(short, [])) & routed, (short, hosts.get(short), routed)


def test_fn_every_service_boots() -> None:
    """Each HTTP service's ``SANDLOOM_MODULES`` load alone (app interpreter)."""
    boot = _boot()
    assert boot and "_error" not in boot, boot.get("_error", "no services")
    bad = {name: r["error"] for name, r in boot.items() if not r["ok"]}
    assert not bad, bad


# --- the rules the framework's deploy-profile test pins ----------------------


def test_dev_no_code_changes() -> None:
    """Config only: no Python of the app changed (generated contract stubs
    excepted)."""
    stubs = f"{root_package()}/shared/contracts/"
    code = [p for p in changed_files() if p.endswith(".py") and not p.startswith(stubs)]
    assert not code, code


def test_dev_database_and_migrate_per_service() -> None:
    services = _services()
    http = _http()
    dbs: dict[str, str] = {}
    for name, svc in http.items():
        env = _env(svc)
        url = env.get("DATABASE_URL", "")
        dbs[name] = url.rsplit("/", 1)[-1]
        migrates = [
            m
            for m, other in services.items()
            if "migrate" in _command(other)
            and _env(other).get("DATABASE_URL") == url
            and _modules(other) == _modules(svc)
        ]
        assert migrates, f"{name}: no migrate one-shot for its database and modules"
        depends = svc.get("depends_on") or {}
        assert any(m in depends for m in migrates), f"{name} does not wait for it"
    assert len(set(dbs.values())) == len(dbs), dbs


def test_dev_bindings_are_exactly_the_consumed_contracts() -> None:
    """Every service binds what its modules consume from ANOTHER service, to
    that service (a missing binding fails at first use, a stale one is dead
    coupling); the token issuer also binds the remote claims contributors."""
    boot = _boot()
    hosts = _hosts()
    http = _http()
    problems: list[str] = []
    for name, svc in http.items():
        if not boot.get(name, {}).get("ok"):
            problems.append(f"{name}: did not boot, its consumed contracts are unknown")
            continue
        consumed = set(boot[name].get("consumed", []))
        expected = {c for c in consumed if set(hosts.get(c.split(".")[0], [])) - {name}}
        if "identity" in {_short(m) for m in _modules(svc)}:
            expected |= {
                f"{c}.claims.v1" for c in CLAIMS if name not in hosts.get(c, [name])
            }
        bound = _bindings(svc)
        if set(bound) != expected:
            problems.append(f"{name}: bound {sorted(bound)}, needs {sorted(expected)}")
        for contract, binding in bound.items():
            owner = hosts.get(contract.split(".")[0], [])
            url = str(binding.get("base_url", "")) if isinstance(binding, dict) else ""
            if owner and not any(
                f"//{o}:" in url or url.endswith(f"//{o}") for o in owner
            ):
                problems.append(f"{name}: {contract} -> {url}, provider {owner}")
    assert not problems, problems


def test_dev_one_beat_for_the_periodic_module() -> None:
    """``reminders`` runs nightly: exactly one beat schedules it, and a worker
    of its service runs it (SANDLOOM-T010)."""
    services = _services().values()
    beats = [s for s in services if "beat" in _command(s) and "celery" in _command(s)]
    workers = [
        s for s in services if "worker" in _command(s) and "celery" in _command(s)
    ]

    def loads_reminders(svc: Any) -> bool:
        mods = {_short(m) for m in _modules(svc)}
        return "reminders" in mods or (
            not mods and _env(svc).get("SANDLOOM_COMPOSITION_ROOT", "") != ""
        )

    assert len([b for b in beats if loads_reminders(b)]) == 1, "beats for reminders"
    assert any(loads_reminders(w) for w in workers), "no worker runs reminders"


def test_dev_durable_event_bus() -> None:
    """Events cross services now (catalog -> enrollment -> progress ...): every
    HTTP service runs the durable bus (``auto`` is streams in prod)."""
    for name, svc in _http().items():
        env = _env(svc)
        assert env.get("ENVIRONMENT", "") == "prod", name
        assert env.get("EVENT_BUS_BACKEND", "auto") in {"auto", "streams"}, name
