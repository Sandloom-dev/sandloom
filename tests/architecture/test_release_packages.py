"""One list of workspace packages feeds every release and packaging tool.

A new distribution is one entry in ``tools/packaging/packages.json`` (plus its own
``packages/<name>/``): these tests add one to a copy of the tooling and run each
tool on it, so no tool compares a package name.
"""

from __future__ import annotations

import fnmatch
import hashlib
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
    "tag_release": True,
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


def _with(entry: dict, **changes: object) -> dict:
    return {**entry, **changes}


GOOD_MODULE = {"name": "sandloom-ok", "kind": "module", "module": "ok"}
GOOD_CONSOLE = {"extra": "ok", "route": "/ok/{path:path}", "with": ["sandloom"]}
BAD_LISTS: dict[str, dict] = {
    "traversal name": {"packages": [_with(GOOD_MODULE, name="../evil")]},
    "mixed case name": {"packages": [_with(GOOD_MODULE, name="sandloom-Admin")]},
    "name with a space": {"packages": [_with(GOOD_MODULE, name="sandloom-a b")]},
    "kernel not named sandloom": {"packages": [{"name": "sandloom-core", "kind": "kernel"}]},
    "non-kernel named sandloom": {"packages": [_with(GOOD_MODULE, name="sandloom")]},
    "unknown kind": {"packages": [_with(GOOD_MODULE, kind="plugin")]},
    "string standard": {"packages": [_with(GOOD_MODULE, standard="false")]},
    "string tag_release": {"packages": [_with(GOOD_MODULE, tag_release="yes")]},
    "module not an identifier": {"packages": [_with(GOOD_MODULE, module="x; import os")]},
    "module with a dot": {"packages": [_with(GOOD_MODULE, module="a.b")]},
    "driver provider injection": {"packages": [
        {"name": "sandloom-d", "kind": "driver", "drivers": {
            "cache.d": {"target": "sandloom_d:D", "provider": "X; import os"}}}
    ]},
    "driver target shape": {"packages": [
        {"name": "sandloom-d", "kind": "driver", "drivers": {
            "cache.d": {"target": "sandloom_d.D", "provider": "CacheProvider"}}}
    ]},
    "driver extra key": {"packages": [
        {"name": "sandloom-d", "kind": "driver", "drivers": {
            "cache.d": {"target": "sandloom_d:D", "provider": "P", "x": "y"}}}
    ]},
    "driver missing provider": {"packages": [
        {"name": "sandloom-d", "kind": "driver", "drivers": {
            "cache.d": {"target": "sandloom_d:D"}}}
    ]},
    "console without route": {"packages": [
        {"name": "sandloom", "kind": "kernel"},
        _with(GOOD_MODULE, console={"extra": "ok", "with": ["sandloom"]}),
    ]},
    "console route injection": {"packages": [
        {"name": "sandloom", "kind": "kernel"},
        _with(GOOD_MODULE, console={**GOOD_CONSOLE, "route": "/x'); import os #"}),
    ]},
    "console with a string": {"packages": [
        {"name": "sandloom", "kind": "kernel"},
        _with(GOOD_MODULE, console={**GOOD_CONSOLE, "with": "sandloom"}),
    ]},
    "console with an unlisted name": {"packages": [
        {"name": "sandloom", "kind": "kernel"},
        _with(GOOD_MODULE, console={**GOOD_CONSOLE, "with": ["sandloom-nope"]}),
    ]},
    "console extra key": {"packages": [
        {"name": "sandloom", "kind": "kernel"},
        _with(GOOD_MODULE, console={**GOOD_CONSOLE, "cmd": "x"}),
    ]},
    "unknown package key": {"packages": [_with(GOOD_MODULE, install="curl x | sh")]},
    "unknown top-level key": {"packages": [], "extra": 1},
    "no packages key": {"_comment": "x"},
    "packages not a list": {"packages": {"sandloom": {}}},
}  # fmt: skip


def test_the_loader_refuses_every_malformed_entry(tooling):
    result = run(
        tooling,
        f"""
        import json, pathlib, release_packages as r
        outcome = {{}}
        for label, document in {BAD_LISTS!r}.items():
            path = pathlib.Path("bad.json")
            path.write_text(json.dumps({{"_comment": "x", **document}}))
            try:
                r.load(path)
                outcome[label] = "loaded"
            except ValueError:
                outcome[label] = "refused"
        # The strict loader still takes the real list and a clean entry.
        good = {{"_comment": "x", "packages": [
            {{"name": "sandloom", "kind": "kernel"}}, {GOOD_MODULE!r}]}}
        path.write_text(json.dumps(good))
        print(json.dumps([outcome, [p.name for p in r.load(path)], len(r.load())]))
        """,
    )
    outcome, good, real = result  # type: ignore[misc]
    assert {label for label, value in outcome.items() if value != "refused"} == set()
    assert good == ["sandloom", "sandloom-ok"]
    assert real >= 6


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


def test_a_tag_push_is_refused_for_a_package_the_environment_does_not_allow(
    tooling, tmp_path
):
    root = tmp_path / "repo"
    for package in [*LIST["packages"], NEW_DRIVER, NEW_MODULE]:
        pyproject = root / "packages" / package["name"] / "pyproject.toml"
        pyproject.parent.mkdir(parents=True)
        pyproject.write_text('[project]\nversion = "0.7.0a2"\n')
    allowed = sorted(p["name"] for p in LIST["packages"] if p.get("tag_release"))
    assert allowed == [
        "sandloom",
        "sandloom-billing",
        "sandloom-identity",
        "sandloom-tenancy",
    ]
    result = run(
        tooling,
        f"""
        import json, pathlib, pypi_release as r
        root = pathlib.Path({str(root)!r})
        outcome = {{}}
        for name in {[p["name"] for p in LIST["packages"]] + ["sandloom-fakemod"]!r}:
            try:
                r.plan_release(
                    root, "", "push", f"refs/tags/{{name}}-v0.7.0a2",
                    dry_run=False, on_main=True,
                )
                outcome[name] = "planned"
            except ValueError as error:
                outcome[name] = str(error)
        for free in ("sandloom-admin; rm -rf /", "../sandloom", "sandloom-Admin", ""):
            try:
                r.plan_release(
                    root, free, "workflow_dispatch", "refs/heads/main",
                    dry_run=True, on_main=True,
                )
                outcome[repr(free)] = "planned"
            except ValueError as error:
                outcome[repr(free)] = str(error)
        print(json.dumps(outcome))
        """,
    )
    outcome: dict[str, str] = result  # type: ignore[assignment]
    planned = sorted(name for name, value in outcome.items() if value == "planned")
    assert planned == allowed
    for name in (
        "sandloom-admin",
        "sandloom-s3",
        "sandloom-standard",
        "sandloom-fakemod",
    ):
        assert "not released by tag" in outcome[name], name
    for free in (
        "'sandloom-admin; rm -rf /'",
        "'../sandloom'",
        "'sandloom-Admin'",
        "''",
    ):
        assert outcome[free] == "Unknown release package", free


def test_the_reviewed_directory_carries_the_list_for_the_privileged_job(
    tooling, tmp_path
):
    """The privileged job has no checkout: what select writes must stand alone."""
    names = [p["name"] for p in [*LIST["packages"], NEW_DRIVER, NEW_MODULE]]
    version = "0.7.0a2"
    validated = tmp_path / "validated"
    validated.mkdir()
    checksums = {}
    for name in names:
        stem = name.replace("-", "_")
        for filename in (
            f"{stem}-{version}-py3-none-any.whl",
            f"{stem}-{version}.tar.gz",
        ):
            data = filename.encode()
            (validated / filename).write_bytes(data)
            checksums[filename] = hashlib.sha256(data).hexdigest()
    (validated / "sha256.json").write_text(json.dumps(checksums))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text(f"## [{version}] - 2026-01-01\n\nNotes.\n")
    reviewed = tmp_path / "reviewed"
    plan = {"packages": names, "versions": dict.fromkeys(names, version)}
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys, pathlib; sys.path.insert(0, {str(tooling)!r})\n"
            "import pypi_release as r\n"
            f"r.select_artifacts({plan!r}, pathlib.Path({str(validated)!r}),"
            f" pathlib.Path({str(reviewed)!r}))",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=repo,
    )
    # Only the reviewed directory is importable: no checkout, no repository.
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(reviewed)!r})\n"
            "import json, pathlib, pypi_release, release_packages\n"
            "listed = release_packages.load(pathlib.Path(sys.path[0]) / 'packages.json')\n"
            "print(json.dumps([[p.name for p in listed], list(pypi_release.PACKAGES),"
            " release_packages.__file__]))",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    loaded, planned, origin = json.loads(done.stdout.strip().splitlines()[-1])
    assert loaded == names == planned
    assert Path(origin).parent == reviewed


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


GUARDED_REDIS = (
    "import importlib\n\n\ndef client():\n    try:\n"
    "        return importlib.import_module('redis')\n"
    "    except ImportError:\n        return None\n"
)
KERNEL_METADATA = (
    "Metadata-Version: 2.4\nName: sandloom\nVersion: 0.1.0\n"
    "Requires-Dist: asyncpg>=0.32; extra == 'postgres'\n"
    "Provides-Extra: postgres\n"
)


def kernel_wheel(path: Path, sources: dict[str, str], metadata: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for package in (
            "sandloom",
            "sandloom/core",
            "sandloom/core/cli",
            "sandloom/core/cli/dev",
        ):
            archive.writestr(f"{package}/__init__.py", "")
        for name, source in sources.items():
            archive.writestr(name, source)
        archive.writestr("sandloom-0.1.0.dist-info/METADATA", metadata)
    return path


DEV_SERVICES = {"sandloom/core/cli/dev/services.py": GUARDED_REDIS}
KERNEL_CASES = {
    "clean": (DEV_SERVICES, KERNEL_METADATA, ""),
    "docstring and comment name a command": (
        {
            **DEV_SERVICES,
            "sandloom/core/a.py": '"""XADD and redis.call( in prose."""\n# XADD\n',
        },
        KERNEL_METADATA,
        "",
    ),
    "redis import": (
        {**DEV_SERVICES, "sandloom/core/a.py": "import redis\n"},
        KERNEL_METADATA,
        "sandloom.core.a:1 imports redis",
    ),
    "redis from-import in a function": (
        {
            **DEV_SERVICES,
            "sandloom/core/a.py": "def f():\n    from redis.asyncio import Redis\n",
        },
        KERNEL_METADATA,
        "sandloom.core.a:2 imports redis.asyncio.Redis",
    ),
    "guarded redis import elsewhere": (
        {**DEV_SERVICES, "sandloom/core/a.py": GUARDED_REDIS},
        KERNEL_METADATA,
        "sandloom.core.a:6 imports redis",
    ),
    "unguarded redis import in the exempt module": (
        {"sandloom/core/cli/dev/services.py": "import redis\n"},
        KERNEL_METADATA,
        "sandloom.core.cli.dev.services:1 imports redis",
    ),
    "exemption gone stale": (
        {"sandloom/core/cli/dev/services.py": "x = 1\n"},
        KERNEL_METADATA,
        "stale exemption",
    ),
    "celery import": (
        {**DEV_SERVICES, "sandloom/core/a.py": "import celery\n"},
        KERNEL_METADATA,
        "sandloom.core.a:1 imports celery",
    ),
    "kombu import by name": (
        {
            **DEV_SERVICES,
            "sandloom/core/a.py": "import importlib\nimportlib.import_module('kombu')\n",
        },
        KERNEL_METADATA,
        "sandloom.core.a:2 imports kombu",
    ),
    "a Redis command": (
        {**DEV_SERVICES, "sandloom/core/a.py": "c.execute_command('XREADGROUP', 1)\n"},
        KERNEL_METADATA,
        "carries Redis protocol code 'XREADGROUP'",
    ),
    "a Lua script": (
        {
            **DEV_SERVICES,
            "sandloom/core/a.py": "S = \"return redis.call('GET', KEYS[1])\"\n",
        },
        KERNEL_METADATA,
        "carries Redis protocol code",
    ),
    "asyncpg is a base requirement": (
        DEV_SERVICES,
        KERNEL_METADATA + "Requires-Dist: asyncpg>=0.32\n",
        "base requirements ['asyncpg']",
    ),
    "a Redis client is a base requirement": (
        DEV_SERVICES,
        KERNEL_METADATA + "Requires-Dist: redis>=5\n",
        "base requirements ['redis']",
    ),
    "a driver package is a base requirement": (
        DEV_SERVICES,
        KERNEL_METADATA + "Requires-Dist: sandloom-celery\n",
        "base requirements ['sandloom-celery']",
    ),
    "asyncpg missing from the postgres extra": (
        DEV_SERVICES,
        "Metadata-Version: 2.4\nName: sandloom\nVersion: 0.1.0\n",
        "asyncpg is not declared by the 'postgres' extra",
    ),
}


def test_the_kernel_wheel_check_bites_on_each_backend_leak(tooling, tmp_path):
    wheels = {
        name: str(kernel_wheel(tmp_path / f"{index}.whl", sources, metadata))
        for index, (name, (sources, metadata, _)) in enumerate(KERNEL_CASES.items())
    }
    result = run(
        tooling,
        f"""
        import json, pathlib, check_package_distributions as c

        def verdict(path):
            try:
                c.check_kernel_backends(pathlib.Path(path))
            except RuntimeError as error:
                return str(error)
            return "ok"

        print(json.dumps({{n: verdict(p) for n, p in {wheels!r}.items()}}))
        """,
    )
    assert isinstance(result, dict)
    assert set(result) == set(KERNEL_CASES)
    for name, (_, _, expected) in KERNEL_CASES.items():
        if expected:
            assert expected in str(result[name]), (name, result[name])
            assert "the kernel wheel is not backend-free" in str(result[name])
        else:
            assert result[name] == "ok", (name, result[name])
