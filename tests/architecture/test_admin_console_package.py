"""The console in the sandloom-admin wheel is the build of ``admin/``."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "src/sandloom_admin/console"
_spec = importlib.util.spec_from_file_location(
    "console_build", ROOT / "tools/packaging/console_build.py"
)
assert _spec and _spec.loader
console_build = importlib.util.module_from_spec(_spec)
sys.modules["console_build"] = console_build
_spec.loader.exec_module(console_build)


def test_the_packaged_console_is_built_from_the_current_source() -> None:
    assert console_build.recorded_hash() == console_build.source_hash(), (
        "admin/ changed since the console was packaged: run `npm run package` "
        "in admin/ and commit src/sandloom_admin/console"
    )


def test_the_packaged_console_ships_no_application_data() -> None:
    assert not (CONSOLE / "admin-manifest.json").exists()
    assert not (CONSOLE / "config.json").exists()


def test_the_packaged_console_loads_only_from_its_own_path() -> None:
    html = (CONSOLE / "index.html").read_text()
    assert re.findall(r'(?:src|href)="(?!/admin/)([^"]+)"', html) == []
    assert "//" not in re.sub(r"https?://www\.w3\.org", "", html.split("<body")[0])


def test_the_source_export_lists_exactly_the_packaged_files() -> None:
    policy = json.loads((ROOT / "tools/packaging/public-source.json").read_text())
    listed = {
        path
        for group in policy["include"]
        for path in group["paths"]
        if path.startswith("src/sandloom_admin/console/")
    }
    packaged = {
        path.relative_to(ROOT).as_posix()
        for path in CONSOLE.rglob("*")
        if path.is_file()
    }
    assert listed == packaged
