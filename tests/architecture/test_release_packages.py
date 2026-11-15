"""One list of workspace packages feeds every release and packaging tool.

A new distribution is one entry in ``tools/packaging/packages.json`` (plus its own
``packages/<name>/``): these tests add one to a copy of the tooling and run each
tool on it, so no tool compares a package name.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TOOLING = ROOT / "tools" / "packaging"
LIST = json.loads((TOOLING / "packages.json").read_text())
NEW_DRIVER = {
    "name": "sandloom-fakedriver",
    "kind": "driver",
    "drivers": {
        "cache.fake": {
            "target": "sandloom_fakedriver:Fake",
            "provider": "CacheProvider",
        }
    },
}
NEW_MODULE = {"name": "sandloom-fakemod", "kind": "module", "module": "fakemod"}


@pytest.fixture
def tooling(tmp_path: Path) -> Path:
    """A copy of the packaging tooling whose list also names the new packages."""
    copy = tmp_path / "tooling"
    shutil.copytree(TOOLING, copy, ignore=shutil.ignore_patterns("__pycache__"))
    extended = {**LIST, "packages": [*LIST["packages"], NEW_DRIVER, NEW_MODULE]}
    (copy / "packages.json").write_text(json.dumps(extended))
    return copy


def run(tooling: Path, code: str) -> object:
    """Run ``code`` (prints one JSON value) with the copied tooling importable."""
    done = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(tooling)!r})\n"
         + textwrap.dedent(code)],
        check=True,
        text=True,
        capture_output=True,
        cwd=tooling,
    )  # fmt: skip
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_list_names_every_workspace_distribution():
    listed = {p["name"] for p in LIST["packages"]}
    on_disk = {p.parent.name for p in (ROOT / "packages").glob("*/pyproject.toml")}
    assert listed == on_disk
    assert sum(p["kind"] == "kernel" for p in LIST["packages"]) == 1


def test_a_new_entry_is_a_release_package_everywhere_in_sync_release(tooling):
    result = run(
        tooling,
        """
        import json, sync_release as s
        text = "sandloom-fakemod>=0.1.0,<0.2\\nhttps://pypi.org/project/sandloom-fakedriver/0.1.0/\\nsandloom-fakemod-v0.1.0\\n"
        print(json.dumps([list(s.PACKAGES), s.synchronized(text, "0.7.0a3")]))
        """,
    )
    packages, text = result  # type: ignore[misc]
    assert {"sandloom-fakedriver", "sandloom-fakemod"} <= set(packages)
    assert text == (
        "sandloom-fakemod>=0.7.0a3,<0.8\n"
        "https://pypi.org/project/sandloom-fakedriver/0.7.0a3/\n"
        "sandloom-fakemod-v0.7.0a3\n"
    )


def test_a_new_entry_is_planned_tagged_and_named_by_pypi_release(tooling, tmp_path):
    root = tmp_path / "repo"
    for package in [*LIST["packages"], NEW_DRIVER, NEW_MODULE]:
        pyproject = root / "packages" / package["name"] / "pyproject.toml"
        pyproject.parent.mkdir(parents=True)
        pyproject.write_text('[project]\nversion = "0.7.0a2"\n')
    result = run(
        tooling,
        f"""
        import json, pathlib, pypi_release as r
        root = pathlib.Path({str(root)!r})
        tag = r.plan_release(
            root, "", "push", "refs/tags/sandloom-fakedriver-v0.7.0a2",
            dry_run=False, on_main=True,
        )
        manual = r.plan_release(
            root, "sandloom-fakemod", "workflow_dispatch", "refs/heads/x",
            dry_run=True, on_main=False,
        )
        everything = r.plan_release(
            root, "all", "workflow_dispatch", "refs/heads/x", dry_run=True, on_main=False
        )
        print(json.dumps([
            tag["packages"], manual["packages"], everything["packages"],
            r.filenames("sandloom-fakemod", "0.7.0a2"),
        ]))
        """,
    )
    tagged, manual, everything, files = result  # type: ignore[misc]
    assert tagged == ["sandloom-fakedriver"]
    assert manual == ["sandloom-fakemod"]
    assert {"sandloom-fakedriver", "sandloom-fakemod"} <= set(everything)
    assert files == [
        "sandloom_fakemod-0.7.0a2-py3-none-any.whl",
        "sandloom_fakemod-0.7.0a2.tar.gz",
    ]


def test_the_reviewed_directory_carries_the_list_for_the_privileged_job():
    source = (TOOLING / "pypi_release.py").read_text()
    for tool in ("release_packages.py", "packages.json"):
        assert tool in source, f"select_artifacts must ship {tool} beside the verifier"


def wheel(path: Path, package: str, entry_points: str | None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{package}/__init__.py", "")
        if entry_points is not None:
            archive.writestr(
                f"{package}-0.1.0.dist-info/entry_points.txt", entry_points
            )
    return path


def test_a_new_entry_is_built_and_checked_by_its_data(tooling, tmp_path):
    good = wheel(
        tmp_path / "driver.whl",
        "sandloom_fakedriver",
        "[sandloom.drivers]\ncache.fake = sandloom_fakedriver:Fake\n",
    )
    wrong = wheel(
        tmp_path / "wrong.whl",
        "sandloom_fakedriver",
        "[sandloom.drivers]\ncache.fake = sandloom_fakedriver:Other\n",
    )
    module = wheel(
        tmp_path / "module.whl",
        "sandloom_fakemod",
        "[sandloom.modules]\nfakemod = sandloom_fakemod\n",
    )
    unregistered = wheel(tmp_path / "unregistered.whl", "sandloom_fakemod", None)
    result = run(
        tooling,
        f"""
        import json, pathlib, check_package_distributions as c

        def verdict(package, path):
            try:
                c._check_contents(package, pathlib.Path(path))
            except RuntimeError as error:
                return str(error)
            return "ok"

        print(json.dumps({{
            "packages": list(c.PACKAGES),
            "modules": c.MODULES["sandloom-fakemod"],
            "driver": verdict("sandloom-fakedriver", {str(good)!r}),
            "wrong driver": verdict("sandloom-fakedriver", {str(wrong)!r}),
            "module": verdict("sandloom-fakemod", {str(module)!r}),
            "no entry point": verdict("sandloom-fakemod", {str(unregistered)!r}),
        }}))
        """,
    )
    assert isinstance(result, dict)
    assert {"sandloom-fakedriver", "sandloom-fakemod"} <= set(result["packages"])
    assert result["modules"] == ["fakemod", "sandloom_fakemod"]
    assert result["driver"] == "ok"
    assert "cache.fake driver entry point" in result["wrong driver"]
    assert result["module"] == "ok"
    assert "sandloom.modules entry point fakemod = sandloom_fakemod" in str(
        result["no entry point"]
    )


def test_every_package_tag_triggers_the_release_workflow():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    patterns = workflow[True]["push"]["tags"]  # YAML reads `on` as True
    for package in [*LIST["packages"], NEW_DRIVER, NEW_MODULE]:
        tag = f"{package['name']}-v0.7.0a2"
        assert any(fnmatch.fnmatchcase(tag, p) for p in patterns), tag
    options = workflow[True]["workflow_dispatch"]["inputs"]["package"]
    assert options["type"] == "string", "a dropdown would repeat the package list"
