"""Budget and integrity of the agent context (DX plan R1).

The root ``AGENTS.md`` is loaded into every agent session (Claude Code via
``CLAUDE.md``; Codex reads at most 32 KiB of instruction files). It stays a
map: one rule, the non-negotiables with the check enforcing each, the golden
path, an index and the intentional oddities. Detail lives in the nested
contracts and ``docs/concepts/``. These tests are the ratchet: grow a
budget only together with an eval run (``benchmarks/dx``) showing it pays.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sandloom.core.cli.scaffold_wiring import API_CHEATSHEET_PATH, MODULE_CONTRACT_PATH

REPO = Path(__file__).resolve().parents[2]

ROOT_MAX_LINES = 150
ROOT_MAX_BYTES = 12 * 1024
#: Everything Claude Code loads at session start in this repository.
ALWAYS_LOADED = ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md")
ALWAYS_LOADED_MAX_BYTES = 16 * 1024
NESTED_MAX_LINES = 120
NESTED_MAX_BYTES = 8 * 1024
#: Directory-scoped contracts; each has a sibling CLAUDE.md importing it.
NESTED = ("src/sandloom", "src/sandloom/core", "deploy", "admin")
RULES_MAX_LINES = 60
#: Paths the root index names before the work that adds them has merged
#: (Public API 1.0, WS-C: the docs rewrite lands these pages).
PENDING_PATHS: set[str] = set()


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_root_agents_md_is_a_map() -> None:
    text = _read("AGENTS.md")
    lines = len(text.splitlines())
    size = len(text.encode())
    assert lines <= ROOT_MAX_LINES and size <= ROOT_MAX_BYTES, (
        f"AGENTS.md is {lines} lines / {size} bytes (budget {ROOT_MAX_LINES} / "
        f"{ROOT_MAX_BYTES}). Move detail to a nested AGENTS.md or "
        "docs/concepts/ and leave one index line."
    )


def test_always_loaded_context_budget() -> None:
    size = sum(
        len(_read(rel).encode()) for rel in ALWAYS_LOADED if (REPO / rel).exists()
    )
    assert size <= ALWAYS_LOADED_MAX_BYTES, (
        f"always-loaded agent context is {size} bytes (budget {ALWAYS_LOADED_MAX_BYTES})"
    )


@pytest.mark.parametrize("directory", NESTED)
def test_nested_contract_budget_and_claude_import(directory: str) -> None:
    text = _read(f"{directory}/AGENTS.md")
    lines, size = len(text.splitlines()), len(text.encode())
    assert lines <= NESTED_MAX_LINES and size <= NESTED_MAX_BYTES, (
        f"{directory}/AGENTS.md is {lines} lines / {size} bytes "
        f"(budget {NESTED_MAX_LINES} / {NESTED_MAX_BYTES})"
    )
    # Claude Code loads nested CLAUDE.md files, not AGENTS.md.
    assert _read(f"{directory}/CLAUDE.md") == "@AGENTS.md\n"


def test_module_contract_template_budget() -> None:
    text = MODULE_CONTRACT_PATH.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= NESTED_MAX_LINES
    assert len(text.encode()) <= NESTED_MAX_BYTES


def test_framework_tree_module_contract_is_the_shipped_template() -> None:
    """``src/sandloom/AGENTS.md`` is what ``sandloom add module`` / ``sandloom new`` ship."""
    assert _read("src/sandloom/AGENTS.md") == MODULE_CONTRACT_PATH.read_text(
        encoding="utf-8"
    ), (
        "src/sandloom/AGENTS.md drifted from the template: edit "
        "sandloom/core/cli/templates/module-AGENTS.md and copy it over"
    )


def test_index_paths_exist() -> None:
    """Every repository path the root map names exists (no dead pointers)."""
    paths = set(
        re.findall(
            r"`((?:Docs|backend|deploy|admin|\.claude)/[^`*<> ]+)`", _read("AGENTS.md")
        )
    )
    missing = sorted(
        p for p in paths if not (REPO / p).exists() and p not in PENDING_PATHS
    )
    assert not missing, f"AGENTS.md points at missing paths: {missing}"
    landed = sorted(p for p in PENDING_PATHS if (REPO / p).exists())
    assert not landed, f"drop {landed} from PENDING_PATHS: they exist now"


def test_path_scoped_rules_are_short_and_scoped() -> None:
    rules = sorted((REPO / ".claude" / "rules").glob("*.md"))
    assert rules
    for rule in rules:
        text = rule.read_text(encoding="utf-8")
        assert text.startswith("---\npaths:"), (
            f"{rule.name}: needs a paths: front matter"
        )
        assert len(text.splitlines()) <= RULES_MAX_LINES, rule.name


def test_root_map_embeds_the_shipped_api_cheatsheet() -> None:
    """The signature cheat-sheet agents read instead of the kernel is one text:
    the root AGENTS.md carries it verbatim, ``sandloom new`` appends the same file."""
    assert API_CHEATSHEET_PATH.read_text(encoding="utf-8") in _read("AGENTS.md"), (
        "AGENTS.md's Public API cheat-sheet drifted from "
        "sandloom/core/cli/templates/api-cheatsheet.md: edit the template, copy it over"
    )
