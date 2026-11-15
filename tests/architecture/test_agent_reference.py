"""The agent reference skills render from code and marked plans, live.

``tools/agent_reference.py <topic>`` prints ``registries``, ``ci``, ``kernel``,
``infra`` and ``plan`` for the checkout it sits in; each of the five
``sandloom-*`` skills (``.agents/skills`` and its ``.claude/skills`` mirror) runs
one of them when it loads. Nothing is generated or committed, so these tests
prove that every topic renders against the real repository, stays small enough
to read whole, that the infrastructure map covers every port and driver entry
point, and that the skill pages call the script with a topic that exists.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "agent_reference.py"
SKILLS = {
    "sandloom-registries": "registries",
    "sandloom-ci-map": "ci",
    "sandloom-kernel-map": "kernel",
    "sandloom-infra-map": "infra",
    "sandloom-plan": "plan",
}


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_reference_under_test", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _tool()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("topic", sorted(SKILLS.values()))
def test_every_topic_renders_and_stays_small(topic: str) -> None:
    text = tool.render(topic, REPO)
    limit = tool.MAX_PLAN_BYTES if topic == "plan" else tool.MAX_BYTES
    assert text.startswith("# "), topic
    size = len(text.encode())
    assert size <= limit, f"{topic}: {size} bytes > {limit}: shorten the section"


def test_the_script_runs_with_a_plain_interpreter_from_the_checkout_root() -> None:
    done = subprocess.run(
        [sys.executable, "-I", str(TOOL), "kernel"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == tool.render("kernel", REPO)


@pytest.mark.parametrize("topic", ["infra", "kernel", "plan", "registries"])
def test_the_script_imports_no_sandloom_module(topic: str) -> None:
    code = (
        "import runpy, sys\n"
        f"sys.argv = ['agent_reference.py', {topic!r}]\n"
        f"runpy.run_path({str(TOOL)!r}, run_name='__main__')\n"
    )
    probe = (
        "import sys\n"
        "try:\n"
        f"    exec({code!r})\n"
        "except SystemExit:\n"
        "    pass\n"
        "print([m for m in sys.modules if m.split('.')[0].startswith('sandloom')])\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith("[]"), done.stdout[-200:]


def test_an_unknown_topic_is_refused() -> None:
    with pytest.raises(SystemExit, match="unknown topic 'nope'"):
        tool.render("nope", REPO)


def test_each_skill_page_runs_its_topic_and_is_mirrored() -> None:
    for skill, topic in SKILLS.items():
        pages = [
            (REPO / base / skill / "SKILL.md").read_text(encoding="utf-8")
            for base in (".agents/skills", ".claude/skills")
        ]
        assert pages[0] == pages[1], f"{skill}: the two copies differ"
        assert f"name: {skill}\n" in pages[0]
        assert f"!`python3 tools/agent_reference.py {topic}`" in pages[0]
        assert "allowed-tools: Bash(python3 tools/agent_reference.py:*)" in pages[0]
        assert "description:" in pages[0]
    commands = set(re.findall(r"tools/agent_reference.py (\w+)`", "".join(pages)))
    assert commands <= set(tool.TOPICS)


def test_the_infrastructure_map_lists_every_port_and_every_driver_entry_point() -> None:
    infra = tool.render("infra", REPO)
    lines = {
        line[2:].split(":", 1)[0]: line
        for line in infra.splitlines()
        if line[:2] == "- "
    }
    ports = {
        p.stem
        for p in (REPO / "src/sandloom/core/infra/ports").glob("*.py")
        if p.stem not in ("_base", "__init__")
    }
    assert ports
    assert ports <= set(lines), f"ports missing from the map: {ports - set(lines)}"
    points = []
    for toml in sorted((REPO / "packages").glob("*/pyproject.toml")):
        entries = tomllib.loads(toml.read_text(encoding="utf-8"))
        found = entries.get("project", {}).get("entry-points", {})
        points += [(toml.parent.name, key) for key in found.get("sandloom.drivers", {})]
    assert points
    for package, key in points:
        cap, _, name = key.partition(".")
        assert f"{name} (" in lines[cap], f"{key} of {package} missing from the map"
        assert f"({package}, " in lines[cap], f"{key}: package {package} not named"
    for cap, line in lines.items():
        kit = REPO / "src/sandloom/driverkit/kits" / f"{cap}.py"
        assert ("Conformance" in line) == kit.is_file(), cap


def _driver_tree(root: Path, target: str, extra: str = "") -> None:
    _write(
        root,
        "src/sandloom/core/drivers/bootstrap.py",
        '_DRIVERS = "pkg.drivers"\n'
        "BUILTIN_DRIVER_ROWS = (\n"
        f'    ("cap", "x", f"{{_DRIVERS}}.{target}", (), "", ""),\n'
        ")\n"
        'BUILTIN_DRIVER_TIERS = {("cap", "x"): "beta"}\n',
    )
    _write(root, "src/pkg/drivers/real.py", "class Cls:\n    tier = 'stable'\n")
    _write(root, "packages/p/pyproject.toml", extra or "[project]\nname = 'p'\n")


def test_driver_rows_resolve_their_f_string_targets_and_tiers(tmp_path: Path) -> None:
    _driver_tree(tmp_path, "real:Cls")
    assert tool._drivers(tmp_path) == {"cap": {"x": ("core", "beta")}}


def test_a_row_without_a_module_file_or_class_stops_the_build(tmp_path: Path) -> None:
    _driver_tree(tmp_path, "nope:Cls")
    with pytest.raises(SystemExit, match="no file for module pkg.drivers.nope"):
        tool._drivers(tmp_path)
    _driver_tree(tmp_path, "real:Missing")
    with pytest.raises(SystemExit, match="has no Missing"):
        tool._drivers(tmp_path)


def test_a_row_set_the_reader_cannot_evaluate_stops_the_build(tmp_path: Path) -> None:
    _driver_tree(tmp_path, "real:Cls")
    path = tmp_path / "src/sandloom/core/drivers/bootstrap.py"
    path.write_text("BUILTIN_DRIVER_ROWS = build_rows()\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="rows or tiers not readable"):
        tool._drivers(tmp_path)


def test_a_package_entry_point_is_listed_with_its_declared_tier(tmp_path: Path) -> None:
    points = (
        "[project]\nname = 'p'\n"
        '[project.entry-points."sandloom.drivers"]\n'
        '"cap.y" = "p_mod:PCls"\n'
    )
    _driver_tree(tmp_path, "real:Cls", points)
    _write(tmp_path, "packages/p/src/p_mod.py", "class PCls:\n    tier = 'beta'\n")
    assert tool._drivers(tmp_path)["cap"]["y"] == ("p", "beta")
    (tmp_path / "packages/p/src/p_mod.py").unlink()
    with pytest.raises(SystemExit, match="no file for module p_mod"):
        tool._drivers(tmp_path)


def test_only_marked_design_docs_are_indexed(tmp_path: Path) -> None:
    marker = tool.MARKER
    _write(
        tmp_path, "design/wave/spec.md", f"{marker}\n# T\n## 1. One (x)\n## 2. Two\n"
    )
    _write(tmp_path, "design/other/plain.md", "# T\n## 1. Hidden\n")
    _write(tmp_path, "design/late/plan.md", "\n\n\n\n\n" + marker + "\n## 1. Late\n")
    text = tool.render("plan", tmp_path)
    assert "`design/wave/spec.md`" in text
    assert "1 One · 2 Two" in text
    assert "Hidden" not in text
    assert "Late" not in text


def test_no_marked_doc_says_so(tmp_path: Path) -> None:
    _write(tmp_path, "design/other/plain.md", "# T\n## 1. Hidden\n")
    assert "no wave is active" in tool.render("plan", tmp_path)


def test_headings_inside_code_fences_are_not_indexed(tmp_path: Path) -> None:
    doc = f"{tool.MARKER}\n## 1. Real\n```\n## 2. Fenced\n```\n## 3. After\n"
    _write(tmp_path, "design/wave/spec.md", doc)
    text = tool.render("plan", tmp_path)
    assert "1 Real · 3 After" in text
    assert "Fenced" not in text
