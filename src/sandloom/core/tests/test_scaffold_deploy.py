"""`sandloom add deploy`: an application splits into services by configuration.

The first run creates the microservices profile and its gateway from the
templates shipped in the package; every run derives the services, bindings,
allowlist, databases and routes from what the modules provide and consume,
rewrites only its generated blocks (hand edits outside them are kept, edits
inside are refused with a diff) and is idempotent. The result passes the same
split invariants the framework's own profile passes (SANDLOOM-T014).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

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
    "probecron/tasks.py": ("def setup_periodic_tasks(sender, **kwargs):\n    pass\n"),
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


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal `sandloom new`-shaped project: probeapp/modules/<probes>."""
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


def _deploy(root: Path, modules: list[str] | None, service: str | None = None, **kw):
    from sandloom.core.cli.scaffold_deploy import Project, deploy

    return deploy(Project.find(root), modules, service, **kw)


def _compose(root: Path) -> dict:
    return yaml.safe_load((root / "deploy" / "compose/microservices.yml").read_text())


def _split(root: Path) -> None:
    _deploy(root, ["identity"], "identity")
    _deploy(root, ["tenancy"])
    _deploy(root, ["probecat"], "catalog")
    _deploy(root, ["probeord"], "orders")


def test_first_deploy_creates_the_profile_gateway_and_init_script(app: Path) -> None:
    result = _deploy(app, ["identity"], "identity")
    deploy = app / "deploy"
    assert {p.relative_to(deploy).as_posix() for p in result.created} == {
        "compose/microservices.yml",
        "gateway/nginx.conf",
        "postgres/init-databases.sh",
    }
    assert (deploy / "postgres" / "init-databases.sh").stat().st_mode & 0o111
    compose = _compose(app)
    common = compose["x-common-env"]
    assert common["CONTRACT_DTO_PACKAGES"] == '["probeapp.shared"]'
    assert common["SANDLOOM_COMPOSITION_ROOT"] == "probeapp.asgi"
    assert common["EVENT_BUS_BACKEND"] == "streams"
    assert compose["x-build"]["context"] == "../.."
    assert compose["x-image"] == "${SANDLOOM_IMAGE:-probeapp:local}"


def test_a_split_app_passes_the_framework_split_invariants(app: Path) -> None:
    from sandloom.core.testing.deploy_profile import profile_problems

    _split(app)
    services = _compose(app)["services"]
    orders = services["orders"]["environment"]
    assert json.loads(orders["SANDLOOM_MODULES"]) == ["probeapp.modules.probeord"]
    assert json.loads(orders["SERVICE_BINDINGS"]) == {
        "probecat.lookup.v1": {
            "base_url": "http://catalog:8000",
            "service_id": "catalog",
        }
    }
    identity = json.loads(services["identity"]["environment"]["SERVICE_BINDINGS"])
    assert identity == {
        "tenancy.claims.v1": {
            "base_url": "http://tenancy:8000",
            "service_id": "tenancy",
        }
    }
    allowed = json.loads(services["catalog"]["environment"]["RPC_ALLOWED_CALLS"])
    assert allowed == {
        "identity": ["tenancy.claims.v1"],
        "orders": ["probecat.lookup.v1"],
    }
    assert (
        "RPC_PRIVATE_KEY" in orders
        and "RPC_PRIVATE_KEY" not in (services["catalog"]["environment"])
    )
    assert services["orders"]["depends_on"]["migrate-orders"] == {
        "condition": "service_completed_successfully"
    }
    assert services["db"]["environment"]["POSTGRES_MULTIPLE_DATABASES"].split() == [
        "probeapp_identity",
        "probeapp_tenancy",
        "probeapp_catalog",
        "probeapp_orders",
    ]
    nginx = (app / "deploy" / "gateway" / "nginx.conf").read_text()
    assert "location = /api/v1/probeord { proxy_pass http://orders; }" in nginx
    assert "location /api/v1/probeord/ { proxy_pass http://orders; }" in nginx
    assert "location /api/v1/platform/probecat/ { proxy_pass http://catalog; }" in nginx
    assert "server orders:8000 resolve;" in nginx
    report = profile_problems(app / "deploy")
    assert report.problems == [] and report.warnings == []


def test_a_root_level_module_gets_the_root_and_the_catch_all(app: Path) -> None:
    """Round 3 (C-DEPLOY-07): ``module_router(prefix="")`` routes ``/`` and
    ``/{slug}`` have no literal prefix. The generated gateway routes ``/``
    (exact) and the catch-all to their service, keeps every internal surface
    a 404, and the checker reads every route the service serves."""
    from sandloom.core.testing.deploy_profile import (
        gateway_locations,
        gateway_route,
        profile_problems,
    )

    _split(app)
    _deploy(app, ["probehome"], "home")
    nginx = (app / "deploy" / "gateway" / "nginx.conf").read_text()
    locations = gateway_locations(nginx)
    assert gateway_route("/", locations)[0] == "home"
    assert gateway_route("/about", locations)[0] == "home"
    assert gateway_route("/api/v1/probeord/ping", locations)[0] == "orders"
    for internal in ("/_sandloom/rpc/x.y.v1", "/metrics", "/ready", "/docs"):
        assert gateway_route(internal, locations) == (None, "404"), internal
    report = profile_problems(app / "deploy")
    assert report.problems == [] and report.warnings == []
    assert {"/", "/{slug}"} <= set(report.topologies["home"].routes)


def test_the_checker_finds_a_root_route_the_gateway_drops(app: Path) -> None:
    """A gateway that 404s the root-level routes (as one generated before
    did) fails the split check: it compares the gateway against the routes
    the service's app really serves, not a prefix-filtered list."""
    from sandloom.core.testing.deploy_profile import profile_problems

    _split(app)
    _deploy(app, ["probehome"], "home")
    nginx = app / "deploy" / "gateway" / "nginx.conf"
    root = re.compile(r"^.*location (= )?/ \{ proxy_pass.*\n", re.M)
    dropped = root.sub("", nginx.read_text()).replace(
        "        # health of the gateway itself",
        "        location / { return 404; }\n        # health of the gateway itself",
    )
    nginx.write_text(dropped)
    problems = profile_problems(app / "deploy").problems
    assert any("routes / (home) to None" in p for p in problems), problems
    assert any("routes /{slug} (home) to None" in p for p in problems), problems


def _home_and_store(app: Path) -> Path:
    """The reviewer's split: home owns ``/`` and ``/{slug}``, store ``/shop``."""
    _split(app)
    _deploy(app, ["probehome"], "home")
    _deploy(app, ["probeshop"], "store")
    return app / "deploy" / "gateway" / "nginx.conf"


@pytest.mark.parametrize(
    ("path", "upstream"),
    [
        ("/shopping", "home"),  # /{slug}, not /shop
        ("/shop", "store"),
        ("/shop/", "store"),
        ("/shop/hat", "store"),
        ("/shopx", "home"),
        ("/api/v1/probeordx", "home"),  # not /api/v1/probeord's
        ("/api/v1/probeord/ping", "orders"),
        ("/api/v1/probeord", "orders"),
        ("/", "home"),
        # identity serves /auth/... but not /auth itself: that is /{slug}
        ("/auth", "home"),
        ("/auth/login", "identity"),
    ],
)
def test_a_route_prefix_matches_only_its_own_segment(
    app: Path, path: str, upstream: str
) -> None:
    """Round 4 (C-DEPLOY-07): every generated prefix location stops at its
    segment boundary (``location = /shop`` and ``location /shop/``), as
    Starlette matches a route: ``/shopping`` is home's ``/{slug}``, not
    store's ``/shop``."""
    from sandloom.core.testing.deploy_profile import (
        gateway_locations,
        gateway_route,
        profile_problems,
    )

    nginx = _home_and_store(app).read_text()
    assert "location /shop { " not in nginx
    assert "location = /shop { proxy_pass http://store; }" in nginx
    assert "location /shop/ { proxy_pass http://store; }" in nginx
    assert gateway_route(path, gateway_locations(nginx))[0] == upstream
    report = profile_problems(app / "deploy")
    assert report.problems == [] and report.warnings == []


def test_the_checker_samples_paths_that_collide_with_another_prefix(
    app: Path,
) -> None:
    """Round 4 (C-DEPLOY-08): the checker fills each path parameter with
    values that collide with other services' literal segments (``shop``,
    ``shopx``) and compares the gateway's upstream with the service whose
    app routes really match the path (Starlette matching, the most literal
    route winning) -- so an unbounded ``location /shop`` that steals
    ``/shopping`` from ``/{slug}`` is found, with no SANDLOOM-T014 conflict."""
    from sandloom.core.testing.deploy_profile import profile_problems

    nginx = _home_and_store(app)
    unbounded = re.sub(
        r"        location = (/[^ ]+) \{ (proxy_pass http://[\w-]+;) \}\n"
        r"        location \1/ \{ \2 \}\n",
        r"        location \1 { \2 }\n",
        nginx.read_text(),
    )
    assert "        location /shop { proxy_pass http://store; }\n" in unbounded
    nginx.write_text(unbounded)
    problems = profile_problems(app / "deploy").problems
    assert any("routes /shopx (home: /{slug}) to store" in p for p in problems), (
        problems
    )
    assert not any("/shop/" in p or "/shop " in p for p in problems), problems


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


def test_rerunning_is_idempotent(app: Path) -> None:
    _split(app)
    before = (app / "deploy" / "compose/microservices.yml").read_text()
    again = _deploy(app, ["probeord"], "orders")
    assert again.written == [] and again.created == [] and again.refusals == []
    assert _deploy(app, None).written == []
    after = (app / "deploy" / "compose/microservices.yml").read_text()
    assert after == before


def test_adding_a_module_is_incremental_and_keeps_hand_edits(app: Path) -> None:
    _split(app)
    profile = app / "deploy" / "compose/microservices.yml"
    nginx = app / "deploy" / "gateway" / "nginx.conf"
    edited = profile.read_text().replace(
        "  METRICS_TOKEN: ${METRICS_TOKEN:-}\n",
        "  METRICS_TOKEN: ${METRICS_TOKEN:-}\n  # my note\n  LOG_LEVEL: DEBUG\n",
    )
    profile.write_text(edited)
    nginx.write_text(
        nginx.read_text().replace(
            "        # health of the gateway itself",
            "        location /legacy { return 410; }\n"
            "        # health of the gateway itself",
        )
    )
    result = _deploy(app, ["probecron"], "orders")
    assert result.refusals == []
    text = profile.read_text()
    assert "  # my note\n  LOG_LEVEL: DEBUG\n" in text
    assert "location /legacy { return 410; }" in nginx.read_text()
    services = _compose(app)["services"]
    assert json.loads(services["orders"]["environment"]["SANDLOOM_MODULES"]) == [
        "probeapp.modules.probeord",
        "probeapp.modules.probecron",
    ]


def test_deploy_patches_a_dockerignore_that_would_ship_the_env_file(
    app: Path,
) -> None:
    """The split writes deploy/.env.microservices (every service's private
    keys) inside the build context: an existing app's root-only .dockerignore
    is patched so no image ships it (or a key file)."""
    from sandloom.core.cli.image_secrets import context_files

    ignore = app / ".dockerignore"
    ignore.write_text(".git\n.env\n.env.*\n!.env.example\n")
    result = _deploy(app, ["identity"], "identity")
    assert ignore in result.written
    (app / "deploy" / ".env.microservices").write_text("JWT_PRIVATE_KEY=x\n")
    (app / "deploy" / "keys").mkdir()
    (app / "deploy" / "keys" / "jwt_private_key").write_text("x\n")
    shipped = context_files(app)
    assert "deploy/.env.microservices" not in shipped
    assert "deploy/keys/jwt_private_key" not in shipped
    assert ignore.read_text().startswith(".git\n.env\n")  # the app's lines kept
    assert ignore not in _deploy(app, ["tenancy"]).written  # once


def test_a_service_added_later_creates_its_own_database(app: Path) -> None:
    """``init-databases.sh`` runs only on an empty Postgres volume: a service
    added to a running stack gets a migrate one-shot that creates its database
    when missing -- also in a profile written before that (the generated
    block carries the command)."""
    import sandloom.core.cli.scaffold_deploy as sd

    _split(app)
    profile = app / "deploy" / "compose/microservices.yml"
    # The profile as written before: no command in the generated one-shots,
    # the anchor's without the flag (shas recorded, so it is not a hand edit).
    older = profile.read_text().replace(', "--create-databases"]', "]")
    older = re.sub(r'^    command: \["sandloom", "migrate".*\n', "", older, flags=re.M)
    bodies = {name: body for name, (body, _sha) in sd.regions(older).items()}
    older, refused = sd.apply_regions(older, bodies, force=True, where="older")
    assert "--create-databases" not in older and refused == []
    profile.write_text(older)
    result = _deploy(app, ["probecron"], "cron")
    assert result.refusals == []
    services = _compose(app)["services"]
    migrates = {n: s for n, s in services.items() if n.startswith("migrate-")}
    assert "migrate-cron" in migrates and len(migrates) >= 2
    for name, svc in migrates.items():
        assert svc["command"] == [
            "sandloom",
            "migrate",
            "upgrade-all",
            "--apply",
            "--create-databases",
        ], name


def test_a_hand_edit_inside_a_generated_block_is_refused_with_a_diff(
    app: Path,
) -> None:
    _split(app)
    profile = app / "deploy" / "compose/microservices.yml"
    original = profile.read_text()
    tampered = original.replace(
        "  SANDLOOM_SERVICE_NAME: orders\n",
        "  SANDLOOM_SERVICE_NAME: orders\n  EXTRA: one\n",
    )
    profile.write_text(tampered)
    result = _deploy(app, ["probecron"], "cron")
    assert result.written == []
    (diff,) = result.refusals
    assert "[service-env] (edited)" in diff and "-  EXTRA: one" in diff
    assert profile.read_text() == tampered  # nothing written
    forced = _deploy(app, ["probecron"], "cron", force=True)
    assert profile in forced.written
    assert "EXTRA: one" not in profile.read_text()


def test_a_module_runs_in_one_service_only(app: Path) -> None:
    from sandloom.core.cli.scaffold_deploy import DeployError

    _split(app)
    with pytest.raises(DeployError, match="already deployed in the service 'catalog'"):
        _deploy(app, ["probecat"], "orders")


def test_a_module_with_periodic_tasks_gets_its_worker_and_one_beat(
    app: Path,
) -> None:
    from sandloom.core.testing.static_checks import periodic_beat_problems

    _split(app)
    _deploy(app, ["probecron"], "cron")
    services = _compose(app)["services"]
    assert {"cron-worker", "cron-beat"} <= set(services)
    assert "orders-worker" not in services and "orders-beat" not in services
    profile = app / "deploy" / "compose/microservices.yml"
    assert periodic_beat_problems(profile) == []


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
def test_the_framework_profile_is_the_generators_output(tmp_path: Path) -> None:
    """The framework's own profile is generated by `sandloom add deploy` too:
    re-deriving it changes nothing (a hand edit inside a block would fail
    here)."""
    from sandloom.core.cli.scaffold_deploy import Project, deploy

    copy = tmp_path / "deploy"
    shutil.copytree(DEPLOY, copy)
    result = deploy(Project.find(REPO, copy))
    assert result.refusals == [] and result.written == [], result.written


@pytest.mark.skipif(not DEPLOY.is_dir(), reason="deploy/ not in this checkout")
def test_the_shipped_templates_match_the_framework_deploy_files() -> None:
    from sandloom.core.cli.scaffold_deploy import TEMPLATES

    script = "postgres/init-databases.sh"
    assert (TEMPLATES / script).read_text() == (DEPLOY / script).read_text()


def test_dev_env_covers_every_required_variable(app: Path) -> None:
    from sandloom.core.testing.deploy_profile import (
        dev_shell,
        env_file_text,
        parse_env_file,
        required_variables,
    )

    _split(app)
    text = (app / "deploy" / "compose/microservices.yml").read_text()
    shell = dev_shell(text)
    assert set(required_variables(text)) <= set(shell)
    rendered = env_file_text(shell, text)
    assert rendered.startswith("# DEVELOPMENT / CI ONLY")
    assert "(required by the profile: set it)" not in rendered
    parsed = parse_env_file(rendered)
    assert json.loads(parsed["RPC_SERVICE_KEYS"]).keys() == {"identity", "orders"}
    pem = parsed["JWT_PRIVATE_KEY"]
    assert pem.startswith("-----BEGIN ") and "\n" not in pem  # one escaped line


def test_keys_dev_env_writes_a_new_private_file(
    app: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sandloom.core.cli.keys import main as keys_main

    monkeypatch.chdir(app)
    assert keys_main(["dev-env"]) == 2  # no profile yet
    _split(app)
    out = app / "deploy" / ".env.microservices"
    assert keys_main(["dev-env", "--out", str(out)]) == 0
    assert out.stat().st_mode & 0o777 == 0o600
    assert "DEVELOPMENT ONLY" in capsys.readouterr().out
    assert keys_main(["dev-env", "--out", str(out)]) == 2  # never overwritten
    assert keys_main(["dev-env"]) == 0
    assert capsys.readouterr().out.startswith("# DEVELOPMENT / CI ONLY")


def test_refresh_preserves_an_older_profiles_runtime_anchor(app: Path) -> None:
    """Existing operator-owned template sections need not contain new anchors."""
    from sandloom.core.cli.scaffold_deploy import apply_regions, regions

    _deploy(app, ["identity"])
    path = app / "deploy/compose/microservices.yml"
    text = path.read_text()
    text = re.sub(r"(?ms)^x-app-runtime:.*?(?=^# Settings shared)", "", text)
    text = text.replace("<<: *app-runtime\n", "<<: *service-runtime\n")
    # Recreate the checksum an earlier generator wrote, not a hand-edited block.
    bodies = {name: body for name, (body, _) in regions(text).items()}
    text, refusals = apply_regions(text, bodies, force=True, where=str(path))
    assert not refusals
    path.write_text(text)
    result = _deploy(app, None)
    assert result.refusals == []
    assert "identity-worker" in _compose(app)["services"]
    assert "*app-runtime" not in path.read_text()
    assert _deploy(app, None).written == []
