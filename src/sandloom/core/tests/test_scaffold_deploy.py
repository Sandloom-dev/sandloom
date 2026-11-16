"""`sandloom add deploy`: an application splits into services by configuration.

The first run creates the microservices profile and its gateway from the
templates shipped in the package; every run derives the services, bindings,
allowlist, databases and routes from what the modules provide and consume,
rewrites only its generated blocks (hand edits outside them are kept, edits
inside are refused with a diff) and is idempotent. The result passes the same
split invariants the framework's own profile passes (SANDLOOM-T014).

The runs that deploy a bundled module next to the probe modules (identity,
tenancy) are in ``tests/integration/with_batteries/test_scaffold_deploy_with_bundled_modules.py``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[4]
DEPLOY = REPO / "deploy"

_RULES = """version: 1
modules_path: probeapp/modules
modules_package: probeapp.modules
source_root: .
"""

_MODULES = {
    # A provider: a contract and an event.
    "probecat/__init__.py": (
        "from sandloom.sdk import define_module\n\n"
        'register = define_module(__name__, version="0.1.0")\n'
    ),
    "probecat/contracts.py": (
        "from sandloom.sdk import contract\n\n\n"
        '@contract("probecat.lookup.v1")\n'
        "class Lookup:\n"
        "    async def ping(self) -> bool:\n"
        "        return True\n"
    ),
    "probecat/api.py": (
        "from sandloom.sdk import CurrentUser, module_router\n\n"
        "router = module_router()\n\n\n"
        '@router.get("/items")\n'
        "async def items(user: CurrentUser) -> list[str]:\n"
        "    return []\n"
    ),
    # A consumer of the provider's contract.
    "probeord/__init__.py": (
        "from sandloom.sdk import define_module\n\n"
        'register = define_module(__name__, version="0.1.0")\n'
    ),
    "probeord/api.py": (
        "from sandloom.core.descriptors.contract_refs import ContractRef\n"
        "from sandloom.sdk import Contract, CurrentUser, module_router\n\n"
        "router = module_router()\n"
        'lookup = Contract(ContractRef("probecat.lookup.v1"))\n\n\n'
        '@router.get("/ping")\n'
        "async def ping(user: CurrentUser) -> bool:\n"
        "    return bool(await lookup.ping())\n"
    ),
    # A module with periodic tasks.
    "probecron/__init__.py": (
        "from sandloom.sdk import define_module\n\n"
        'register = define_module(__name__, version="0.1.0")\n'
    ),
    "probecron/tasks.py": (
        "from sandloom.sdk.tasks import periodic\n\n\n"
        '@periodic(cron="0 * * * *", id="probecron.tick")\n'
        "async def tick() -> None:\n    return None\n"
    ),
    # Root-level routes ("/" and a catch-all "/{slug}"), and a second module
    # claiming a root-level path too.
    "probehome/__init__.py": (
        "from sandloom.sdk import define_module\n\n"
        'register = define_module(__name__, version="0.1.0")\n'
    ),
    "probehome/api.py": (
        "from sandloom.sdk import CurrentUser, module_router\n\n"
        'router = module_router(prefix="")\n\n\n'
        '@router.get("/")\n'
        "async def home(user: CurrentUser) -> str:\n"
        '    return "home"\n\n\n'
        '@router.get("/{slug}")\n'
        "async def page(slug: str, user: CurrentUser) -> str:\n"
        "    return slug\n"
    ),
    # A literal root-level prefix (/shop) beside the catch-all's /{slug}.
    "probeshop/__init__.py": (
        "from sandloom.sdk import define_module\n\n"
        'register = define_module(__name__, version="0.1.0")\n'
    ),
    "probeshop/api.py": (
        "from sandloom.sdk import CurrentUser, module_router\n\n"
        'router = module_router(prefix="")\n\n\n'
        '@router.get("/shop")\n'
        "async def shop(user: CurrentUser) -> str:\n"
        '    return "shop"\n\n\n'
        '@router.get("/shop/{item}")\n'
        "async def item(item: str, user: CurrentUser) -> str:\n"
        "    return item\n"
    ),
    "probeland/__init__.py": (
        "from sandloom.sdk import define_module\n\n"
        'register = define_module(__name__, version="0.1.0")\n'
    ),
    "probeland/api.py": (
        "from sandloom.sdk import CurrentUser, module_router\n\n"
        'router = module_router(prefix="")\n\n\n'
        '@router.get("/{lang}/welcome")\n'
        "async def welcome(lang: str, user: CurrentUser) -> str:\n"
        "    return lang\n"
    ),
}


def probe_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A minimal `sandloom new`-shaped project: probeapp/modules/<probes>
    (the ``app`` fixture here and in the suite that adds the bundled modules)."""
    root = tmp_path / "probeapp_root"
    package = root / "probeapp"
    (package / "modules").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "asgi.py").write_text("")
    (package / "modules" / "__init__.py").write_text("")
    for rel, source in _MODULES.items():
        path = package / "modules" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    (root / "architecture_rules.yml").write_text(_RULES)
    (root / "Dockerfile").write_text("FROM scratch\nEXPOSE 8000\n")
    monkeypatch.syspath_prepend(str(root))
    yield root
    for name in [m for m in sys.modules if m.startswith("probeapp")]:
        del sys.modules[name]


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield from probe_app(tmp_path, monkeypatch)


def _deploy(root: Path, modules: list[str] | None, service: str | None = None, **kw):
    from sandloom.core.cli.scaffold_deploy import Project, deploy

    return deploy(Project.find(root), modules, service, **kw)


def test_two_services_claiming_root_level_paths_conflict(app: Path) -> None:
    """Two services with root-level routes cannot share the gateway's single
    root: the deploy is refused with SANDLOOM-T014 naming both, never silently
    dropped."""
    from sandloom.core.cli.scaffold_deploy import DeployError

    _deploy(app, ["probehome"], "home")
    with pytest.raises(DeployError, match=r"root-level.*home.*land.*SANDLOOM-T014"):
        _deploy(app, ["probeland"], "land")


def test_a_hand_written_catch_all_outside_the_block_is_refused(app: Path) -> None:
    """The catch-all is generated: a static ``location /`` kept outside the
    routes block would be a duplicate nginx refuses at start."""
    from sandloom.core.cli.scaffold_deploy import DeployError

    _deploy(app, ["probecat"], "catalog")
    nginx = app / "deploy" / "gateway" / "nginx.conf"
    nginx.write_text(
        nginx.read_text().replace(
            "        # health of the gateway itself",
            "        location / { return 404; }\n        # health of the gateway itself",
        )
    )
    with pytest.raises(DeployError, match=r"location / .*outside the generated"):
        _deploy(app, None)


def _service_infra(root: Path) -> dict[str, dict[str, Any]]:
    import json

    import yaml

    text = (root / "deploy" / "compose" / "microservices.yml").read_text()
    data = yaml.safe_load(text)
    return {
        key.removeprefix("x-").removesuffix("-env"): json.loads(env["INFRA"])
        for key, env in data.items()
        if key.startswith("x-")
        and key.endswith("-env")
        and key != "x-common-env"
        and "INFRA" in env
    }


def _infra_problems(root: Path) -> list[str]:
    from sandloom.core.cli.scaffold_deploy import Project
    from sandloom.core.testing.deploy_profile import profile_problems

    problems = profile_problems(Project.find(root).deploy_dir).problems
    return [p for p in problems if "INFRA" in p]


def test_each_service_gets_the_shared_infra_and_its_own_broker(app: Path) -> None:
    """A service's INFRA is the shared one plus its Celery broker (one Redis
    database per service); the split invariants accept exactly that and still
    refuse any other difference."""
    from sandloom.core.cli.scaffold_deploy import shared_infra

    _deploy(app, ["probehome"], "home")
    _deploy(app, ["probecat"], "catalog")
    compose = app / "deploy" / "compose" / "microservices.yml"
    shared = shared_infra(compose.read_text())
    assert shared["cache"]["default"]["url"] == "redis://redis:6379/0"
    per_service = _service_infra(app)
    assert set(per_service) == {"home", "catalog"}
    brokers = sorted(
        infra["taskqueue"]["default"]["broker"] for infra in per_service.values()
    )
    assert brokers == ["redis://redis:6379/2", "redis://redis:6379/3"]
    for infra in per_service.values():
        assert {k: v for k, v in infra.items() if k != "taskqueue"} == shared
    assert not _infra_problems(app)
    # Idempotent, and a refresh keeps each service's broker database.
    before = compose.read_text()
    _deploy(app, None)
    assert compose.read_text() == before
    # A shared edit differs from the services' copies until a refresh
    # re-copies it: a difference other than the broker is a problem.
    marker = "# >>> generated by sandloom add deploy: service-env"
    shared_part, generated = before.split(marker)
    old = '"cache": {"default": {"url": "redis://redis:6379/0"}}'
    assert old in shared_part and old in generated
    # Only the shared INFRA (x-common-env, above the generated services) changes.
    compose.write_text(
        shared_part.replace(old, old.replace("/0", "/9"), 1) + marker + generated
    )
    assert "/9" not in str(_service_infra(app)["home"]["cache"])
    _deploy(app, None)
    assert "/9" in str(_service_infra(app)["home"]["cache"])
    assert not _infra_problems(app)


def test_a_service_infra_differing_beyond_the_broker_is_a_problem(app: Path) -> None:
    _deploy(app, ["probehome"], "home")
    _deploy(app, ["probecat"], "catalog")
    assert not _infra_problems(app)
    compose = app / "deploy" / "compose" / "microservices.yml"
    text = compose.read_text()
    marker = "# >>> generated by sandloom add deploy: service-env"
    head, generated = text.split(marker)
    changed = generated.replace("redis://redis:6379/0", "redis://redis:6379/5", 1)
    assert changed != generated
    compose.write_text(head + marker + changed)
    problems = _infra_problems(app)
    assert any("INFRA differs between processes" in p for p in problems), problems


def test_a_profile_from_before_infra_carried_the_broker_keeps_its_databases(
    app: Path,
) -> None:
    """``BROKER_URL`` in a generated block (written before) is read for its
    database: the refresh does not renumber the services onto the cache's."""
    import re

    _deploy(app, ["probehome"], "home")
    compose = app / "deploy" / "compose" / "microservices.yml"
    marker = "# >>> generated by sandloom add deploy: service-env"
    head, generated = compose.read_text().split(marker)
    legacy = re.sub(
        r"  INFRA: '\{.*\}'\n",
        "  BROKER_URL: redis://redis:6379/7\n",
        generated,
        count=1,
    )
    assert legacy != generated
    compose.write_text(head + marker + legacy)
    _deploy(app, None, force=True)
    broker = _service_infra(app)["home"]["taskqueue"]["default"]["broker"]
    assert broker == "redis://redis:6379/7"


def test_a_service_with_no_broker_gets_the_next_free_database_not_the_caches(
    app: Path,
) -> None:
    """A service naming neither an INFRA broker nor a BROKER_URL is unknown: a
    refresh assigns it the next free database (as a new service gets), never
    ``/0``, which is the cache's."""
    import re

    _deploy(app, ["probehome"], "home")
    _deploy(app, ["probecat"], "catalog")
    compose = app / "deploy" / "compose" / "microservices.yml"
    marker = "# >>> generated by sandloom add deploy: service-env"
    head, generated = compose.read_text().split(marker)
    bare = re.sub(r"  INFRA: '\{.*\}'\n", "", generated, count=1)
    assert bare != generated
    compose.write_text(head + marker + bare)
    _deploy(app, None, force=True)
    brokers = {
        name: infra["taskqueue"]["default"]["broker"]
        for name, infra in _service_infra(app).items()
    }
    assert sorted(brokers.values()) == ["redis://redis:6379/3", "redis://redis:6379/4"]
    assert not any(url.endswith("/0") for url in brokers.values())


def test_a_shared_infra_that_is_not_json_is_refused(app: Path) -> None:
    from sandloom.core.cli.scaffold_deploy import DeployError

    _deploy(app, ["probehome"], "home")
    compose = app / "deploy" / "compose" / "microservices.yml"
    text = compose.read_text()
    start = text.index('  INFRA: \'{"atomic_store"')
    end = text.index("\n", start)
    compose.write_text(text[:start] + "  INFRA: 'not json'" + text[end:])
    with pytest.raises(DeployError, match="not a JSON object"):
        _deploy(app, None)


def test_main_refuses_without_modules_or_refresh(
    app: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sandloom.core.cli.scaffold_deploy import main

    monkeypatch.chdir(app)
    with pytest.raises(SystemExit):
        main([])
    assert main(["--refresh"]) == 1  # no profile yet
    assert "creates it" in capsys.readouterr().err
    assert main(["probecat", "--service", "catalog"]) == 0
    assert "created" in capsys.readouterr().out


@pytest.mark.skipif(not DEPLOY.is_dir(), reason="deploy/ not in this checkout")
def test_the_shipped_templates_match_the_framework_deploy_files() -> None:
    from sandloom.core.cli.scaffold_deploy import TEMPLATES

    script = "postgres/init-databases.sh"
    assert (TEMPLATES / script).read_text() == (DEPLOY / script).read_text()
