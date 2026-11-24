"""The example apps teach the current path: no stale "until it lands" markers.

An example is what a reader copies. A comment that says the right form is not
available yet (``advanced db() lands with H1``), or an untyped service lookup
(``get_core().get_service("...")``), teaches the way around a gap that is
closed. This scan fails on them, so a stale marker cannot come back unseen.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
SKIPPED_PARTS = {"node_modules", ".venv", "__pycache__", ".sandloom"}
SUFFIXES = {".py", ".md", ".yml", ".yaml", ".toml"}

STALE_PATTERNS: dict[str, re.Pattern[str]] = {
    "a 'lands with H1' marker": re.compile(r"lands\s+with\s+H1", re.IGNORECASE),
    "an 'until db()' marker": re.compile(r"until\s+db\(\)"),
    # The stub-less lookup in any spelling: `get_core().get_service(...)`, and a
    # `core: Any = Depends(get_core)` parameter followed by `core.get_service(`.
    "an untyped .get_service(...) lookup": re.compile(r"\.\s*get_service\("),
    # Only the untyped parameter: a `core: CoreRuntime = Depends(get_core)` that
    # calls a typed runtime method (`collect_claims`) is current code.
    "an untyped core: Any = Depends(get_core) parameter": re.compile(
        r":\s*Any\s*=\s*Depends\(\s*get_core\s*\)"
    ),
}


def example_files(root: Path = EXAMPLES) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in SUFFIXES
        and not SKIPPED_PARTS.intersection(path.relative_to(root).parts)
    )


def stale_markers(files: list[Path], root: Path) -> list[str]:
    found: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for label, pattern in STALE_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{path.relative_to(root)}:{line}: {label}")
    return found


def test_scan_sees_the_examples() -> None:
    files = example_files()
    names = {path.name for path in files}
    assert {"ticket_service.py", "project_service.py", "link_service.py"} <= names
    assert len(files) > 100


def test_examples_carry_no_stale_marker() -> None:
    found = stale_markers(example_files(), ROOT)
    assert not found, "stale markers in the examples:\n" + "\n".join(found)


@pytest.mark.parametrize(
    ("source", "label"),
    [
        (
            "from sandloom.sdk.web import DbSession"
            "  # sandloom: advanced db() lands with H1\n",
            "'lands with H1'",
        ),
        ("# raw session until db() is available\n", "'until db()'"),
        (
            'membership = get_core().get_service("tenancy.membership.v1")\n',
            ".get_service(",
        ),
        (
            "core = get_core()\n    .get_service(\n",
            ".get_service(",
        ),
        (
            "def route(core: Any = Depends(get_core)) -> None: ...\n",
            "Depends(get_core)",
        ),
    ],
)
def test_scan_flags_a_broken_example(tmp_path: Path, source: str, label: str) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text(source, encoding="utf-8")
    found = stale_markers(example_files(tmp_path), tmp_path)
    assert len(found) == 1 and label in found[0], found


def test_scan_flags_the_original_helpdesk_lookup(tmp_path: Path) -> None:
    # The code helpdesk carried before this scan, verbatim: the lookup went
    # through a `core` parameter, not a `get_core()` call.
    old = tmp_path / "tickets.py"
    old.write_text(
        "def _membership_service(core: Any = Depends(get_core)) -> Any:\n"
        "    try:\n"
        '        return core.get_service("tenancy.membership.v1")\n'
        "    except Exception as exc:\n"
        "        raise HTTPException(status_code=501) from exc\n",
        encoding="utf-8",
    )
    found = stale_markers(example_files(tmp_path), tmp_path)
    assert len(found) == 2, found
    assert any(".get_service(" in line for line in found)
    assert any("Depends(get_core)" in line for line in found)


def test_scan_accepts_the_current_path(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(
        "from sandloom.sdk import Contract, db\n"
        "members = Contract(TENANCY_MEMBERSHIP_V1)\n",
        encoding="utf-8",
    )
    assert stale_markers(example_files(tmp_path), tmp_path) == []
