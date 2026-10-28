"""`sandloom new` project generator tests.

A generated app must be a *standalone* project that consumes the framework as a
dependency: its own namespace, valid Python, a derived module list (every
module of ``<app>.modules``), Public API 1.0 code (``sandloom.sdk``), and modules
that never absolute-import the app's own package.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli.project import generate_project


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text()


def test_generate_minimal_project(tmp_path: Path) -> None:
    created = generate_project("probe", tmp_path, sample_module="gadgets")
    root = tmp_path / "probe"
    assert created

    # Core project files exist.
    for rel in (
        "pyproject.toml",
        "README.md",
        ".env.example",
        ".env.test",
        "probe/asgi.py",
        "probe/modules/__init__.py",
        "tests/test_smoke.py",
        "probe/modules/gadgets/migrations/versions/0001_gadgets_baseline.py",
        "architecture_rules.yml",
        "deploy/docker-compose.yml",
        "probe/modules/gadgets/__init__.py",
    ):
        assert (root / rel).exists(), f"missing {rel}"

    # Every generated .py file parses.
    for path in created:
        if path.suffix == ".py":
            ast.parse(path.read_text())


def test_minimal_app_loads_only_its_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No module list to edit: the composition root derives it (G-MODULE-19)."""
    from sandloom.sdk.app import app_modules

    generate_project("probe", tmp_path, sample_module="gadgets")
    root = tmp_path / "probe"
    assert "SANDLOOM_MODULES=" not in _read(root, ".env.test")
    assert "BUNDLED: list[str] = []" in _read(root, "probe/asgi.py")
    monkeypatch.syspath_prepend(str(root))
    # Another test in this process may have imported a different "probe" app.
    for name in [m for m in sys.modules if m == "probe" or m.startswith("probe.")]:
        monkeypatch.delitem(sys.modules, name)
    assert app_modules("probe.modules") == ["probe.modules.gadgets"]


def test_with_batteries_orders_modules(tmp_path: Path) -> None:
    generate_project(
        "shop",
        tmp_path,
        with_batteries=["billing", "identity"],  # given out of order
        sample_module="catalog",
    )
    root = tmp_path / "shop"
    # canonical order is identity, tenancy (skipped), billing; then app
    # modules. Installed modules are named by their entry-point names.
    asgi = _read(root, "shop/asgi.py")
    assert 'BUNDLED: list[str] = ["identity", "billing"]' in asgi
    assert 'app_modules("shop.modules")' in asgi


def test_apps_depend_on_sandloom_and_each_chosen_module_package(
    tmp_path: Path,
) -> None:
    import tomllib

    # Each --with module is its own ecosystem package, a compatible range.
    generate_project("shop", tmp_path, with_batteries=["identity"])
    shop = tomllib.loads(_read(tmp_path / "shop", "pyproject.toml"))
    assert shop["project"]["dependencies"] == [
        "sandloom[redis,celery]>=0.7.0a1,<0.8",
        "sandloom-identity>=0.7.0a1,<0.8",
    ]

    # A minimal app depends on the kernel alone (+ its Redis/Celery drivers:
    # the generated compose runs redis and the Celery worker).
    generate_project("probe", tmp_path)
    probe = tomllib.loads(_read(tmp_path / "probe", "pyproject.toml"))
    assert probe["project"]["dependencies"] == ["sandloom[redis,celery]>=0.7.0a1,<0.8"]


def test_generated_app_installs_the_fast_extra(tmp_path: Path) -> None:
    # The native accelerators the framework's own image runs: an optional
    # `fast` extra (sandloom[fast]) that dev installs and the generated
    # Dockerfile exports, so tests and production run the same engines.
    import tomllib

    generate_project("probe", tmp_path)
    root = tmp_path / "probe"
    data = tomllib.loads(_read(root, "pyproject.toml"))
    extras = data["project"]["optional-dependencies"]
    assert extras["fast"] == ["sandloom[fast]"]
    assert "probe[fast]" in extras["dev"]
    dockerfile = _read(root, "Dockerfile")
    assert "uv export --frozen --no-dev --extra fast" in dockerfile
    assert "uv pip compile pyproject.toml --extra fast" in dockerfile

    # An explicit override wins over both.
    generate_project(
        "custom",
        tmp_path,
        with_batteries=["identity"],
        framework_req="sandloom==0.7.1",
    )
    custom_pyproject = _read(tmp_path / "custom", "pyproject.toml")
    assert '"sandloom==0.7.1"' in custom_pyproject


def test_asgi_reuses_framework_factory(tmp_path: Path) -> None:
    generate_project("probe", tmp_path)
    asgi = _read(tmp_path / "probe", "probe/asgi.py")
    # The kernel factory (sandloom.sdk.app; sandloom.main is the standard root).
    assert "from sandloom.sdk.app import app_modules, create_app" in asgi
    assert "sandloom.main" not in asgi
    assert "app = create_app(default_modules=" in asgi


def test_generated_module_is_namespace_portable(tmp_path: Path) -> None:
    generate_project("probe", tmp_path, sample_module="gadgets")
    mod_root = tmp_path / "probe" / "probe" / "modules" / "gadgets"
    for path in mod_root.rglob("*.py"):
        # The module's Alembic chain (migrations/) is a standalone runner: it
        # names its own models by absolute path (the prefix is baked in at
        # generation time) and uses the core migration helper — both legitimate.
        if "migrations" in path.parts:
            continue
        for line in path.read_text().splitlines():
            s = line.strip()
            if s.startswith(("import sandloom", "from sandloom")):
                assert s.startswith(("from sandloom.sdk", "import sandloom.sdk")), (
                    f"non-1.0 framework import in {path}: {s}"
                )
            assert "probe.modules.gadgets" not in s or path.name == "__init__.py"


def test_rejects_bad_app_name(tmp_path: Path) -> None:
    for bad in ("Probe", "1app", "has-dash", "", "class", "async", "app\n"):
        with pytest.raises(ValueError):
            generate_project(bad, tmp_path)


@pytest.mark.parametrize("name", ["class", "async", "orders\n"])
def test_rejects_invalid_sample_module_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        generate_project("probe", tmp_path, sample_module=name)
    assert not (tmp_path / "probe").exists()


def test_rejects_unknown_battery(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        generate_project("probe", tmp_path, with_batteries=["analytics"])


def test_refuses_overwrite(tmp_path: Path) -> None:
    generate_project("probe", tmp_path)
    with pytest.raises(FileExistsError):
        generate_project("probe", tmp_path)


# --------------------------------------------------------------------------- #
# Generated deploy artefacts must actually start (regression: ENVIRONMENT was
# "production" -> Settings ValidationError; build context had no Dockerfile; no
# migrate step; JWT placeholder accepted in prod).
# --------------------------------------------------------------------------- #
def _compose(root: Path) -> dict[str, Any]:
    import yaml

    loaded = yaml.safe_load(_read(root, "deploy/docker-compose.yml"))
    assert isinstance(loaded, dict)
    return loaded


def _interpolate(value: object, env: dict[str, str]) -> str:
    """Minimal docker-compose ``${VAR}`` / ``${VAR:-d}`` / ``${VAR:?msg}``."""

    def repl(m: re.Match[str]) -> str:
        name, op, arg = m.group(1), m.group(2), m.group(3)
        if env.get(name):
            return env[name]
        if op == ":-":
            return arg or ""
        raise AssertionError(f"compose requires {name}: {arg}")

    return re.sub(r"\$\{(\w+)(?:(:[-?])([^}]*))?\}", repl, str(value))


def test_generated_deploy_files_exist_and_build_from_project_root(
    tmp_path: Path,
) -> None:
    generate_project("probe", tmp_path)
    root = tmp_path / "probe"
    for rel in ("Dockerfile", ".dockerignore", ".env", "deploy/docker-compose.yml"):
        assert (root / rel).exists(), rel
    compose = _compose(root)
    for svc in ("app", "migrate"):
        build = compose["services"][svc]["build"]
        built_from = (
            root / "deploy" / build["context"] / build["dockerfile"]
        ).resolve()
        assert built_from == (root / "Dockerfile").resolve()
    migrate = compose["services"]["migrate"]
    assert migrate["command"] == ["sandloom", "migrate", "upgrade-all", "--apply"]
    assert compose["services"]["app"]["depends_on"]["migrate"] == {
        "condition": "service_completed_successfully"
    }
    dockerfile = _read(root, "Dockerfile")
    assert "USER appuser" in dockerfile
    assert "probe.asgi:app" in dockerfile
    assert ".env\n" in _read(root, ".dockerignore")


def test_generated_compose_env_validates_against_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import secrets

    from sandloom.core.config.settings import Settings

    generate_project("probe", tmp_path)
    compose = _compose(tmp_path / "probe")
    shell = {
        "JWT_SECRET": secrets.token_hex(32),
        "INTERNAL_API_TOKEN": secrets.token_hex(32),
        "TWOFA_ENCRYPTION_KEY": secrets.token_hex(32),
    }
    for svc in ("app", "migrate", "worker", "beat"):
        env = {
            k: _interpolate(v, shell)
            for k, v in compose["services"][svc]["environment"].items()
        }
        assert env["ENVIRONMENT"] == "prod"
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        cfg = Settings(_env_file=None)  # type: ignore[call-arg]
        assert cfg.is_prod
        assert cfg.sandloom_modules == []  # derived by the composition root
        assert cfg.contract_dto_packages == ["probe.shared"]


def test_env_files_secret_handling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import ValidationError

    from sandloom.core.config.settings import Settings

    generate_project("probe", tmp_path)
    root = tmp_path / "probe"
    example = _read(root, ".env.example")
    local = _read(root, ".env")
    placeholder = "change_me_in_production_use_openssl_rand_hex_32"
    assert f"JWT_SECRET={placeholder}" in example
    secret_line = next(ln for ln in local.splitlines() if ln.startswith("JWT_SECRET="))
    assert placeholder not in secret_line
    assert len(secret_line.split("=", 1)[1]) == 64
    # The placeholder must never pass production validation.
    for key, value in {
        "ENVIRONMENT": "prod",
        "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
        "CACHE_URL": "redis://h:6379/0",
        "BROKER_URL": "redis://h:6379/1",
        "JWT_SECRET": placeholder,
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(_env_file=None)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# D4: the generated manifest is the one `sandloom check` reads (keys + public API)
# --------------------------------------------------------------------------- #
def test_generated_manifest_is_enforced_by_sandloom_check(tmp_path: Path) -> None:
    from sandloom.core.archcheck import load_rules, run_checks

    generate_project("probe", tmp_path, sample_module="gadgets")
    root = tmp_path / "probe"
    rules = load_rules(root / "architecture_rules.yml")
    assert rules.modules_package == "probe.modules"
    assert rules.business_modules() == ["gadgets"]
    # module_api / projections are part of the enforced public API.
    assert {"sandloom.core.module_api", "sandloom.core.projections"} <= rules.public_api
    assert run_checks(rules) == [], "a fresh project must pass sandloom check"

    from sandloom.core.cli import add

    add.add_module("widgets", add.layout(root))
    svc = root / "probe" / "modules" / "widgets" / "leak.py"
    svc.write_text(
        "from probe.modules.gadgets.models import GadgetsExample\n"
        "from sandloom_identity.services.token_service import TokenService\n"
        "from sandloom.core._internal.db_provider import get_db\n"
        "async def f(db):\n    await db.commit()\n"
    )
    found = {v.rule for v in run_checks(rules) if v.file == svc}
    assert {
        "module-boundaries",
        "framework-imports",
        "infra-imports",
        "uow-boundary",
    } <= found


def test_sandloom_check_cli_exit_codes(tmp_path: Path) -> None:
    from sandloom.core.cli.check import main as check_main

    generate_project("probe", tmp_path, sample_module="gadgets")
    rules = tmp_path / "probe" / "architecture_rules.yml"
    assert check_main(["--rules", str(rules)]) == 0
    assert check_main(["--rules", str(tmp_path / "missing.yml")]) == 2
    assert check_main(["--rules", str(rules), "--only", "nope"]) == 2
    leak = tmp_path / "probe" / "probe" / "modules" / "gadgets" / "x.py"
    leak.write_text("async def f(db):\n    await db.rollback()\n")
    assert check_main(["--rules", str(rules)]) == 1


def test_the_former_manifest_key_is_refused(tmp_path: Path) -> None:
    """`allowed_framework_imports` is refused, never silently ignored."""
    from sandloom.core.archcheck import load_rules
    from sandloom.core.archcheck.manifest import ManifestError

    (tmp_path / "app" / "modules" / "m").mkdir(parents=True)
    (tmp_path / "app" / "modules" / "m" / "__init__.py").write_text("")
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text(
        "modules_path: app/modules\nallowed_framework_imports: [sandloom.core.di, sandloom.shared]\n"
    )
    with pytest.raises(ManifestError, match="public_api"):
        load_rules(manifest)


def test_generated_ci_and_precommit_run_sandloom_check(tmp_path: Path) -> None:
    import yaml

    generate_project("probe", tmp_path)
    root = tmp_path / "probe"
    ci = yaml.safe_load(_read(root, ".github/workflows/ci.yml"))
    steps = [s.get("run", "") for s in ci["jobs"]["checks"]["steps"]]
    assert any("sandloom test --ci" in run for run in steps)  # sandloom check included
    hooks = yaml.safe_load(_read(root, ".pre-commit-config.yaml"))
    ids = [h["id"] for repo in hooks["repos"] for h in repo["hooks"]]
    assert "sandloom-check" in ids


def test_dev_tool_pins_match_framework() -> None:
    """Generated apps pin the framework's own ruff/mypy/pytest versions."""
    from sandloom.core.cli.project import DEV_TOOL_PINS

    pyproject = Path(__file__).resolve().parents[4] / "pyproject.toml"
    if not pyproject.exists():  # installed wheel: no source tree to compare
        pytest.skip("framework pyproject.toml not available")
    pins = {
        m.group(1).lower()
        for m in re.finditer(
            r'^\s*"([A-Za-z0-9_.-]+==[^"]+)"', pyproject.read_text(), re.M
        )
    }
    for pin in DEV_TOOL_PINS:
        assert pin.lower() in pins, f"{pin} drifted from the framework pins"


# --------------------------------------------------------------------------- #
# D3/D6: worker + beat services; no project-level Alembic chain
# --------------------------------------------------------------------------- #
def test_compose_has_framework_worker_and_beat(tmp_path: Path) -> None:
    generate_project("probe", tmp_path)
    services = _compose(tmp_path / "probe")["services"]
    for svc in ("worker", "beat"):
        cmd = services[svc]["command"]
        assert cmd[:4] == ["celery", "-A", "sandloom.core.worker", svc]
        assert services[svc]["environment"]["SANDLOOM_COMPOSITION_ROOT"] == "probe.asgi"
        assert services[svc]["depends_on"]["migrate"] == {
            "condition": "service_completed_successfully"
        }


def test_identity_apps_require_twofa_key(tmp_path: Path) -> None:
    generate_project("shop", tmp_path, with_batteries=["identity"])
    env = _compose(tmp_path / "shop")["services"]["worker"]["environment"]
    with pytest.raises(AssertionError, match="TWOFA_ENCRYPTION_KEY"):
        _interpolate(env["TWOFA_ENCRYPTION_KEY"], {})
    generate_project("probe", tmp_path)
    plain = _compose(tmp_path / "probe")["services"]["app"]["environment"]
    assert "TWOFA_ENCRYPTION_KEY" not in plain


def test_billing_apps_must_choose_a_psp(tmp_path: Path) -> None:
    generate_project("shop", tmp_path, with_batteries=["billing"])
    env = _compose(tmp_path / "shop")["services"]["app"]["environment"]
    assert ":?" in env["BILLING_PROVIDER"]
    with pytest.raises(AssertionError, match="BILLING_PROVIDER"):
        _interpolate(env["BILLING_PROVIDER"], {})
    generate_project("probe", tmp_path)
    plain = _compose(tmp_path / "probe")["services"]["app"]["environment"]
    assert "BILLING_PROVIDER" not in plain


def test_no_project_level_alembic_chain(tmp_path: Path) -> None:
    generate_project("probe", tmp_path, sample_module="gadgets")
    root = tmp_path / "probe"
    assert not (root / "alembic").exists()
    assert not (root / "alembic.ini").exists()
    chain = root / "probe" / "modules" / "gadgets" / "migrations"
    assert (chain / "env.py").exists()
    assert (chain / "versions" / "0001_gadgets_baseline.py").exists()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("tenant_rls", [False, True])
def test_upgrade_all_on_freshly_generated_project(
    tmp_path: Path, tenant_rls: bool
) -> None:
    """`sandloom new` + `sandloom add module` + `sandloom migrate upgrade-all --apply` on a
    brand-new database: the core chain (packaged with the framework) and both
    module chains apply, and a second run is an idempotent no-op."""
    import os
    import subprocess
    import uuid

    import psycopg2
    from sqlalchemy.engine import make_url

    from sandloom.core.cli import add
    from sandloom.core.testing.workers import worker_env_values

    # The TEST database (CI exports DATABASE_URL; locally .env.test) —
    # never the `settings` snapshot, which comes from the dev .env.
    env_test = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")
    test_env = {**env_test, **os.environ}
    test_url = str(test_env.get("DATABASE_URL") or "")
    if not test_url:
        pytest.skip("no test DATABASE_URL configured")
    base = make_url(test_url.replace("+asyncpg", ""))
    if base.get_backend_name() != "postgresql":
        pytest.skip("needs the Postgres test service")
    dbname = f"{base.database}_gen_{uuid.uuid4().hex[:8]}"
    admin = {
        "host": base.host,
        "port": base.port,
        "user": base.username,
        "password": base.password,
        "dbname": base.database,
    }
    try:
        conn = psycopg2.connect(**admin)
    except psycopg2.OperationalError:
        pytest.skip("Postgres test service not reachable")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{dbname}"')
    conn.close()

    try:
        generate_project("probe", tmp_path, sample_module="gadgets")
        root = tmp_path / "probe"
        add.add_module("widgets", add.layout(root))
        async_url = base.set(
            database=dbname, drivername="postgresql+asyncpg"
        ).render_as_string(hide_password=False)
        env = {
            **os.environ,
            "PYTHONPATH": str(root),
            "SANDLOOM_COMPOSITION_ROOT": "probe.asgi",
            "SANDLOOM_MODULES": json.dumps(
                ["probe.modules.gadgets", "probe.modules.widgets"]
            ),
            "ENVIRONMENT": "test",
            "DATABASE_URL": async_url,
            "CACHE_URL": str(test_env.get("CACHE_URL") or "redis://localhost:6380/0"),
            "BROKER_URL": str(test_env.get("BROKER_URL") or "redis://localhost:6380/1"),
            "JWT_SECRET": "x" * 40,
            "EVENT_BUS_BACKEND": "memory",
        }
        env["INFRA"] = json.dumps(
            {
                "database": {
                    "default": {
                        "url": async_url,
                        "tenant_rls": tenant_rls,
                    }
                }
            }
        )
        cli = [sys.executable, "-m", "sandloom.core.cli.migrate"]
        for _ in range(2):  # the second run must be a no-op
            proc = subprocess.run(
                [*cli, "upgrade-all", "--apply"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert proc.returncode == 0, proc.stdout + proc.stderr

        conn = psycopg2.connect(**{**admin, "dbname": dbname})
        with conn.cursor() as cur:
            cur.execute(
                "select table_name from information_schema.tables "
                "where table_schema = 'public'"
            )
            tables = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname IN ('gadgets_items', 'widgets_items')"
            )
            assert set(cur.fetchall()) == {
                ("gadgets_items", tenant_rls, tenant_rls),
                ("widgets_items", tenant_rls, tenant_rls),
            }
            cur.execute(
                "SELECT tablename FROM pg_policies WHERE schemaname = 'public' "
                "AND tablename IN ('gadgets_items', 'widgets_items')"
            )
            assert {r[0] for r in cur.fetchall()} == (
                {"gadgets_items", "widgets_items"} if tenant_rls else set()
            )
            # Exercise the policies as an ordinary role, not the test superuser.
            # The role and probe rows roll back when this connection closes.
            role = f"generated_app_{uuid.uuid4().hex}"
            tenant = str(uuid.uuid4())
            cur.execute(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS')
            cur.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
            cur.execute(f'GRANT SELECT, INSERT ON gadgets_items TO "{role}"')
            cur.execute(f'SET LOCAL ROLE "{role}"')
            if tenant_rls:
                cur.execute(
                    "SELECT set_config('sandloom.tenant_id', %s, true)", (tenant,)
                )
            cur.execute(
                "INSERT INTO gadgets_items(id, tenant_id, owner_id, title) "
                "VALUES (%s, %s, %s, 'probe')",
                (str(uuid.uuid4()), tenant, str(uuid.uuid4())),
            )
            cur.execute("SELECT count(*) FROM gadgets_items")
            assert cur.fetchone()[0] == 1
            if tenant_rls:
                cur.execute(
                    "SELECT set_config('sandloom.tenant_id', %s, true)",
                    (str(uuid.uuid4()),),
                )
                cur.execute("SELECT count(*) FROM gadgets_items")
                assert cur.fetchone()[0] == 0
        conn.close()
        assert {"outbox_messages", "audit_log", "consumer_inbox"} <= tables
        assert {"gadgets_items", "widgets_items"} <= tables
    finally:
        conn = psycopg2.connect(**admin)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.close()


@pytest.mark.parametrize(
    "module", ["example", "notifications", "customer_notifications"]
)
def test_generated_project_passes_its_own_lint(tmp_path: Path, module: str) -> None:
    """The generated CI runs `ruff check .` — a fresh project must be clean."""
    import subprocess

    generate_project(
        "shop", tmp_path, with_batteries=["identity", "billing"], sample_module=module
    )
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-fix", "--no-cache", "."],
        cwd=tmp_path / "shop",
        capture_output=True,
        text=True,
    )
    if "No module named ruff" in proc.stderr:
        pytest.skip("ruff not installed")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_generated_app_activates_the_test_plugin(tmp_path: Path) -> None:
    """Apps get the harness from `-p sandloom.sdk.testing.plugin`, not conftests."""
    generate_project("probe", tmp_path, sample_module="gadgets")
    root = tmp_path / "probe"
    assert '"-p", "sandloom.sdk.testing.plugin"' in _read(root, "pyproject.toml")
    assert not list(root.rglob("conftest.py"))


def test_generated_app_fails_on_deprecation_warnings(tmp_path: Path) -> None:
    """A generated app's suite turns DeprecationWarning into an error, so a
    deprecated framework call (e.g. a session passed to a contract method) is
    caught before the next release turns it into a TypeError."""
    generate_project("probe", tmp_path)
    pyproject = _read(tmp_path / "probe", "pyproject.toml")
    assert 'filterwarnings = ["error::DeprecationWarning"]' in pyproject


@pytest.mark.integration
@pytest.mark.slow
def test_generated_project_passes_its_own_tests(tmp_path: Path) -> None:
    """`sandloom new` + `sandloom add module` -> the generated tests pass as generated.

    No conftest anywhere: the app's pytest addopts load the framework plugin,
    which loads .env.test, boots the composition root and serves the
    ``client`` / ``db`` fixtures to both modules' tests.

    Public API 1.0 after hand-off H1: the 1.0 sample module calls ``db()``
    and its routes are model E (no operation class); no failure is allowed —
    the suite is green as generated.
    """
    import os
    import subprocess
    import uuid

    import psycopg2
    from sqlalchemy.engine import make_url

    from sandloom.core.cli import add
    from sandloom.core.testing.workers import worker_env_values

    env_test = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")
    test_env = {**env_test, **os.environ}
    test_url = str(test_env.get("DATABASE_URL") or "")
    if not test_url:
        pytest.skip("no test DATABASE_URL configured")
    base = make_url(test_url.replace("+asyncpg", ""))
    if base.get_backend_name() != "postgresql":
        pytest.skip("needs the Postgres test service")
    dbname = f"{base.database}_app_{uuid.uuid4().hex[:8]}"
    admin = {
        "host": base.host,
        "port": base.port,
        "user": base.username,
        "password": base.password,
        "dbname": base.database,
    }
    try:
        conn = psycopg2.connect(**admin)
    except psycopg2.OperationalError:
        pytest.skip("Postgres test service not reachable")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{dbname}"')
    conn.close()

    try:
        generate_project("probe", tmp_path, sample_module="gadgets")
        root = tmp_path / "probe"
        add.add_module("widgets", add.layout(root))
        async_url = base.set(
            database=dbname, drivername="postgresql+asyncpg"
        ).render_as_string(hide_password=False)
        # Point the generated .env.test at the throwaway database + both modules.
        lines = []
        for line in _read(root, ".env.test").splitlines():
            key = line.split("=", 1)[0]
            if key == "DATABASE_URL":
                line = f"DATABASE_URL={async_url}"
            elif key in ("CACHE_URL", "BROKER_URL") and test_env.get(key):
                line = f"{key}={test_env[key]}"
            elif key == "SANDLOOM_MODULES":
                line = 'SANDLOOM_MODULES=["probe.modules.gadgets", "probe.modules.widgets"]'
            lines.append(line)
        (root / ".env.test").write_text("\n".join(lines) + "\n")
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SANDLOOM_", "PYTEST_")) and k != "INFRA"
        }
        # The nested suite shares this run's Redis indexes: it runs as the
        # same owner (sandloom.core.testing.ownership), or it refuses them.
        from sandloom.core.testing.ownership import OWNER_ENV, current_owner

        env[OWNER_ENV] = current_owner()
        report = tmp_path / "report.xml"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={report}",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        out = proc.stdout + proc.stderr
        # The junit report, read with a regex (it is our own pytest's output).
        cases = re.findall(
            r"<testcase (.*?)(?:/>|>(.*?)</testcase>)", report.read_text(), re.S
        )
        passed, failed = 0, []
        for attrs, body in cases:
            if "<failure" not in body and "<error" not in body:
                passed += 1
            else:
                failed.append(f"{attrs}: {body[:2000]}")
        assert not failed, "\n".join(failed) + out
        # 2 smoke tests and the 2 generated tests of each module (the sample
        # and widgets, both from `sandloom add module`).
        assert passed >= 6, out
    finally:
        conn = psycopg2.connect(**admin)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.close()


def test_generated_project_ships_the_agent_contract(tmp_path: Path) -> None:
    """``sandloom new`` writes the 1.0 agent map at the project root (and a
    CLAUDE.md importing it): the commands and the 29 beginner names (spec
    §9.1); its modules inherit it instead of copying it."""
    import sandloom.sdk
    import sandloom.sdk.testing

    generate_project("probe", tmp_path, sample_module="gadgets")
    root = tmp_path / "probe"
    agents = (root / "AGENTS.md").read_text()
    for command in ("sandloom add module", "sandloom migrate new", "sandloom test"):
        assert command in agents
    for name in [
        *sandloom.sdk.__beginner__,
        *sandloom.sdk.testing.__beginner__,
        "client",
    ]:
        assert f"`{name}`" in agents, name
    assert "probe/modules" in agents
    assert (root / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert not (root / "probe" / "modules" / "gadgets" / "AGENTS.md").exists()


def test_generated_project_ships_the_app_skills(tmp_path: Path) -> None:
    """``sandloom new`` writes the app's agent skills for Claude Code and other
    agents, rewritten for the app; the map names them in one line
    (ledger C-GEN-13)."""
    from sandloom.core.cli.skills import APP_SKILLS, SKILL_DIRS

    generate_project("probe", tmp_path)
    root = tmp_path / "probe"
    agents = (root / "AGENTS.md").read_text()
    for name in APP_SKILLS:
        assert name in agents
        for directory in SKILL_DIRS:
            text = (root / directory / name / "SKILL.md").read_text()
            assert text.startswith(f"---\nname: {name}\n")
            assert "<app>" not in text
            assert "backend/" not in text  # app paths, not the framework tree's
    gates = (root / ".claude/skills/sandloom-quality-gates/SKILL.md").read_text()
    assert "probe/shared/contracts" in gates
    assert "uv run sandloom test" in gates


def test_sandloom_add_skills_refreshes_an_existing_app(tmp_path: Path) -> None:
    from sandloom.core.cli.add import main as add_main

    generate_project("probe", tmp_path)
    root = tmp_path / "probe"
    skill = root / ".agents/skills/sandloom-new-module/SKILL.md"
    original = skill.read_text()
    (root / ".claude/skills/sandloom-split-deploy/SKILL.md").unlink()
    assert add_main(["--root", str(root), "skills"]) == 0
    assert (root / ".claude/skills/sandloom-split-deploy/SKILL.md").is_file()
    skill.write_text(original + "\nmy note\n")
    assert add_main(["--root", str(root), "skills"]) == 1  # an edited file is kept
    assert skill.read_text().endswith("my note\n")
    assert add_main(["--root", str(root), "skills", "--force"]) == 0
    assert skill.read_text() == original
