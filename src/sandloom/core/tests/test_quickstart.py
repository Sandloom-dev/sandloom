"""The quickstart: one source (``project.QUICKSTART``), the docs and the
generated README match it, ``sandloom test`` runs without pytest-xdist, the dev
services' ports are configurable, ``--framework-path`` takes the repository
root, and ``sandloom new`` writes the generated files.

The quickstart itself runs end to end in the CI job ``quickstart``
(``tools/smoke/quickstart_check.py``); ``test_quickstart_end_to_end`` runs that
script when ``SANDLOOM_QUICKSTART_POSTGRES_URL`` is set.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from sandloom.core.cli import run_tests
from sandloom.core.cli.project import (
    DEV_TOOL_PINS,
    QUICKSTART,
    generate_project,
    quickstart,
    write_generated_files,
)

BACKEND = Path(__file__).resolve().parents[4]
REPO = BACKEND
DOCS = REPO / "docs" / "getting-started" / "index.md"


def _docs_block() -> list[str]:
    text = DOCS.read_text(encoding="utf-8")
    begin = text.index("<!-- quickstart:begin")
    end = text.index("<!-- quickstart:end -->")
    return text[begin:end].splitlines()[1:]


def test_the_docs_quickstart_is_what_sandloom_new_prints() -> None:
    if not DOCS.is_file():
        pytest.skip("no source tree (installed wheel)")
    assert _docs_block() == ["```bash", *quickstart("myapp"), "```"]


def test_the_quickstart_covers_the_inner_loop() -> None:
    commands = [command for command, _ in QUICKSTART]
    assert commands[:2] == ["cd {path}", "uv sync"]
    assert "uv run sandloom add module {module}" in commands
    assert "uv run sandloom test" in commands
    assert commands[-1].startswith("uv run uvicorn {app}.asgi:app")
    assert not any("pytest" in command for command in commands)
    assert quickstart("shop", "notes")[3].startswith("uv run sandloom add module tasks")
    assert quickstart("shop", path="/w/shop")[0] == "cd /w/shop"


def test_a_long_cd_path_does_not_pad_the_comments() -> None:
    """The comments align on the commented commands, whatever the ``cd`` path."""
    short = quickstart("shop")
    long = quickstart("shop", path="/" + "very/long/" * 12 + "shop")
    assert long[1:] == short[1:]
    assert long[0].startswith("cd /very/long/")


def test_the_readme_repeats_the_quickstart(tmp_path: Path) -> None:
    generate_project("probe", tmp_path, framework_source="pypi")
    readme = (tmp_path / "probe" / "README.md").read_text(encoding="utf-8")
    for line in quickstart("probe")[1:]:
        assert line in readme
    assert "SANDLOOM_DEV_POSTGRES_PORT" in readme


def test_dev_service_ports_are_configurable(tmp_path: Path) -> None:
    generate_project("probe_app", tmp_path, framework_source="pypi")
    compose = (tmp_path / "probe_app" / "deploy" / "docker-compose.dev.yml").read_text(
        encoding="utf-8"
    )
    for var, default in (
        ("SANDLOOM_DEV_POSTGRES_PORT", 5432),
        ("SANDLOOM_DEV_REDIS_PORT", 6379),
        ("SANDLOOM_DEV_TEST_POSTGRES_PORT", 5433),
        ("SANDLOOM_DEV_TEST_REDIS_PORT", 6380),
    ):
        assert f"${{{var}:-{default}}}" in compose
    # Its own compose project: two apps' dev stacks never share containers.
    assert "\nname: probe-app-dev\n" in compose


def test_the_app_depends_on_pytest_xdist() -> None:
    assert any(pin.startswith("pytest-xdist==") for pin in DEV_TOOL_PINS)


def test_framework_path_takes_the_repository_root(tmp_path: Path) -> None:
    if not (REPO / "packages").is_dir():
        pytest.skip("no source tree (installed wheel)")
    generate_project("rooted", tmp_path, framework_source="path", framework_path=REPO)
    pyproject = (tmp_path / "rooted" / "pyproject.toml").read_text(encoding="utf-8")
    package = json.dumps(str((BACKEND / "packages" / "sandloom").resolve()))
    # Editable: the app always runs the checkout's current tree (a non-editable
    # path source is a copy uv rebuilds only when the package's pyproject changes).
    assert f"sandloom = {{ path = {package}, editable = true }}" in pyproject
    with pytest.raises(ValueError, match="framework repository directory"):
        generate_project(
            "nowhere", tmp_path, framework_source="path", framework_path=tmp_path
        )


def test_every_sourced_distribution_is_a_direct_dependency(tmp_path: Path) -> None:
    """uv honours ``[tool.uv.sources]`` for direct dependencies only: a
    batteries app on an unpublished checkout must name every framework
    distribution it sources, or ``uv sync`` looks sandloom-billing up
    in the registry and fails."""
    if not (REPO / "packages").is_dir():
        pytest.skip("no source tree (installed wheel)")
    generate_project(
        "batteries",
        tmp_path,
        with_batteries=["identity", "tenancy", "billing"],
        framework_source="path",
        framework_path=REPO,
    )
    pyproject = tomllib.loads(
        (tmp_path / "batteries" / "pyproject.toml").read_text(encoding="utf-8")
    )
    deps = {
        re.split(r"[\[<>=!~ ]", dep, maxsplit=1)[0]
        for dep in pyproject["project"]["dependencies"]
    }
    sourced = set(pyproject["tool"]["uv"]["sources"])
    assert sourced == {
        "sandloom",
        "sandloom-identity",
        "sandloom-tenancy",
        "sandloom-billing",
    }
    assert sourced <= deps, f"{sourced - deps} sourced but not direct dependencies"


@pytest.mark.slow
def test_an_editable_framework_install_is_the_live_tree(tmp_path: Path) -> None:
    """The editable install a ``--framework-source path`` app gets copies no
    ``sandloom`` file (a copy would shadow the tree and go stale): its ``.pth`` puts
    ``packages/editable/sandloom`` (only ``sandloom``, linked to
    ``src/sandloom``) on ``sys.path``: no other distribution's package."""
    packages = BACKEND / "packages"
    if not (packages / "sandloom").is_dir() or shutil.which("uv") is None:
        pytest.skip("no source tree (installed wheel) or no uv")
    editable = packages / "editable" / "sandloom"
    assert (editable / "sandloom").resolve() == (BACKEND / "src" / "sandloom").resolve()
    assert sorted(p.name for p in editable.iterdir()) == ["sandloom"]
    venv = tmp_path / "venv"
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    subprocess.run(["uv", "venv", "-q", str(venv)], check=True, env=env)
    install = ["uv", "pip", "install", "-q", "--no-deps"]
    install += ["--python", str(venv / "bin" / "python")]
    install += ["-e", str(packages / "sandloom")]
    subprocess.run(install, check=True, env=env)
    (site,) = venv.glob("lib/python*/site-packages")
    assert not (site / "sandloom").exists()
    pth = (site / "_sandloom.pth").read_text().split()
    assert [Path(p).resolve() for p in pth] == [editable.resolve()]


def test_sandloom_test_runs_serially_without_xdist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = run_tests.plan(
        BACKEND, None, full=False, gates=True, workers="auto", xdist=False
    )
    by_name = {step.name: step for step in steps}
    suite = by_name["suite (not slow)"]
    assert "-n" not in suite.argv and "--dist" not in suite.argv
    assert suite.argv[-2:] == ["-m", "not slow"]
    assert "SANDLOOM_TEST_REDIS_DB_STRIDE" not in suite.env
    if "architecture tests" in by_name:
        assert "-n" not in by_name["architecture tests"].argv
    parallel = run_tests.plan(
        BACKEND, None, full=False, gates=False, workers="2", xdist=True
    )
    assert parallel[0].argv[-6:] == ["-n", "2", "--dist", "loadfile", "-m", "not slow"]

    monkeypatch.setattr(run_tests, "xdist_available", lambda: False)
    monkeypatch.chdir(tmp_path)
    assert run_tests.main(["--plan", "--no-gates"]) == 0
    out = capsys.readouterr()
    assert run_tests.XDIST_MISSING_NOTICE in out.err
    assert " -n " not in out.out


def test_sandloom_new_writes_the_generated_files(tmp_path: Path) -> None:
    generate_project("fresh", tmp_path, framework_source="pypi")
    root = tmp_path / "fresh"
    assert write_generated_files(root) is None
    module = root / "fresh" / "modules" / "example"
    assert (module / "module.snapshot.json").is_file()
    assert (module / "routes.snapshot.json").is_file()
    assert list((root / "fresh" / "shared" / "events").glob("example.*.json"))


@pytest.mark.slow
def test_quickstart_end_to_end(tmp_path: Path) -> None:
    """The CI job's script; opt-in (it needs uv, network and servers)."""
    postgres = os.environ.get("SANDLOOM_QUICKSTART_POSTGRES_URL")
    if not postgres:
        pytest.skip(
            "set SANDLOOM_QUICKSTART_POSTGRES_URL (+ _REDIS_URL, _REDIS_DB_BASE)"
        )
    argv = [
        sys.executable,
        str(REPO / "tools" / "smoke" / "quickstart_check.py"),
        "--workdir",
        str(tmp_path / "work"),
        "--postgres-url",
        postgres,
        "--redis-url",
        os.environ.get("SANDLOOM_QUICKSTART_REDIS_URL", "redis://localhost:6380"),
        "--redis-db-base",
        os.environ.get("SANDLOOM_QUICKSTART_REDIS_DB_BASE", "0"),
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert "quickstart: OK" in proc.stdout
