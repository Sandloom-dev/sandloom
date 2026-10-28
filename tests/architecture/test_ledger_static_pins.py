"""Static pins that close gaps found by the guarantee ledger (``guarantees.yml``).

Each test pins one promise that was documented but had no enforcing test. The
ledger entry it closes is named in the docstring.
"""

from __future__ import annotations

import configparser
import re
import tomllib
from collections import Counter
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND
SRC = BACKEND / "src"


def test_orm_leakage_baseline_stays_empty() -> None:
    """G-MODULE-11: the legacy ORM-leakage ratchet is paid off; nothing re-enters it."""
    text = (REPO / "tests" / "architecture" / "test_api_no_orm_leakage.py").read_text()
    assert re.search(r"^ORM_LEAKAGE_BASELINE: set\[str\] = set\(\)$", text, re.M), (
        "ORM_LEAKAGE_BASELINE must stay empty — map ORM rows to DTOs in services"
    )


def test_import_contracts_have_no_ignore_imports() -> None:
    """G-KERNEL-04: no kernel import contract carries an ``ignore_imports`` escape."""
    parser = configparser.ConfigParser()
    parser.read(BACKEND / ".importlinter", encoding="utf-8")
    offenders = [s for s in parser.sections() if parser.has_option(s, "ignore_imports")]
    assert offenders == [], f"ignore_imports is debt (ADR-0007): {offenders}"


def test_framework_deprecations_fail_its_suite() -> None:
    """G-KERNEL-12: a DeprecationWarning raised from ``sandloom`` code fails the suite."""
    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    filters = pyproject["tool"]["pytest"]["ini_options"]["filterwarnings"]
    assert "error::DeprecationWarning:sandloom" in filters


def test_no_distribution_registers_a_pytest11_plugin() -> None:
    """C-MODULE-08: installing Sandloom never changes another project's test suite.

    Apps opt into the harness explicitly (``-p sandloom.core.testing.plugin``).
    """
    files = [
        BACKEND / "pyproject.toml",
        *sorted((BACKEND / "packages").glob("*/pyproject.toml")),
    ]
    for path in files:
        project = tomllib.loads(path.read_text()).get("project", {})
        assert "pytest11" not in project.get("entry-points", {}), path


def test_backend_image_is_locked_and_non_root() -> None:
    """G-OPS-20: the image installs from the locked ``uv.lock`` and runs as non-root."""
    dockerfile = (REPO / "deploy" / "docker" / "Dockerfile").read_text()
    assert "uv sync --locked" in dockerfile
    users = re.findall(r"^USER\s+(\S+)", dockerfile, re.M)
    assert users, "the image must declare a USER"
    assert users[-1] not in {"root", "0"}, f"the final USER is {users[-1]}"


def test_monolith_migrate_one_shot_gates_every_process() -> None:
    """G-OPS-16: in the monolith profile app / worker / beat start only after
    ``migrate`` (``sandloom-migrate upgrade-all --apply``) completed successfully."""
    yaml = pytest.importorskip("yaml")
    path = REPO / "deploy" / "compose/monolith.yml"
    if not path.is_file():
        pytest.skip("deploy/ profiles not in this checkout")
    services = yaml.safe_load(path.read_text())["services"]
    assert services["migrate"]["command"] == [
        "sandloom",
        "migrate",
        "upgrade-all",
        "--apply",
    ]
    for name in ("app", "worker", "beat"):
        assert services[name]["depends_on"]["migrate"] == {
            "condition": "service_completed_successfully"
        }, name


#: Raw environment reads (``os.getenv`` / ``os.environ``) in framework runtime
#: code, per file. AGENTS.md: configuration is read through ``Settings``; these
#: are the existing process-env reads (the composition root, the gunicorn
#: config, test/bootstrap switches). The count may only go DOWN (G-CONFIG-06).
RAW_ENV_BASELINE: dict[str, int] = {
    "sandloom_billing/migrations/versions/0005_billing_subscription_origin.py": 1,
    "sandloom/core/app_factory.py": 1,
    "sandloom/core/auth/hashing.py": 2,
    "sandloom/core/cli/extract.py": 1,
    "sandloom/core/cli/gunicorn_conf.py": 14,
    "sandloom/core/cli/project.py": 1,
    "sandloom/core/composition.py": 1,
    "sandloom/core/config/settings.py": 4,
    "sandloom/core/migrations/alembic_support.py": 1,
    "sandloom/core/migrations/migrations.py": 3,
    "sandloom/core/observability/metrics.py": 1,
    "sandloom/core/tasks/relay.py": 1,
    "sandloom/core/worker.py": 1,
}
_RAW_ENV = re.compile(r"os\.getenv|os\.environ\.get|os\.environ\[")


def test_raw_environment_reads_only_go_down() -> None:
    """G-CONFIG-06: no NEW ``os.getenv`` in framework code — add a ``Settings`` field."""
    counts: Counter[str] = Counter()
    # The kernel and the standard module packages (src/sandloom_*).
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if "/tests/" in rel or rel.startswith("sandloom/core/testing/"):
            continue
        n = len(_RAW_ENV.findall(path.read_text(encoding="utf-8")))
        if n:
            counts[rel] = n
    grown = {
        f: (RAW_ENV_BASELINE.get(f, 0), n)
        for f, n in counts.items()
        if n > RAW_ENV_BASELINE.get(f, 0)
    }
    assert not grown, (
        f"new raw env reads (baseline, now): {grown} — read it from Settings"
    )
    shrunk = {
        f: (n, counts.get(f, 0))
        for f, n in RAW_ENV_BASELINE.items()
        if counts.get(f, 0) < n
    }
    assert not shrunk, f"raw env reads paid down — lower RAW_ENV_BASELINE: {shrunk}"
