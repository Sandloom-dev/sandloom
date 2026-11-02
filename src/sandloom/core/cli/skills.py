"""The agent files an ``sandloom new`` application ships (``sandloom add skills``).

The framework's procedures, rewritten for an application's paths and
commands (packaged under ``sandloom/core/cli/templates/skills/``): map a
module, create a module, run the gates, roll an event to ``.v2``, split into
services. Each coding agent reads skills from its own folder
(:data:`CODING_AGENTS`): ``.claude/skills`` for Claude Code, ``.agents/skills``
for most others, ``.kiro/skills`` for Kiro. The application chooses its agents
once (:func:`detect_agents` proposes the installed ones) and the choice is kept
in :data:`RECORD`.

The record also holds a hash of every file the framework wrote, so ``sandloom
add skills`` replaces a file it wrote that a later version changed, and keeps
a file you edited unless ``force``. The module-author contract
(``<modules>/AGENTS.md``) is managed the same way.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import sandloom.core.cli.scaffold_wiring as _wiring

#: The skills an application gets, in the order the map lists them.
APP_SKILLS: tuple[str, ...] = (
    "sandloom-module-context",
    "sandloom-new-module",
    "sandloom-quality-gates",
    "sandloom-event-version-roll",
    "sandloom-split-deploy",
)
_TEMPLATES = Path(__file__).resolve().parent / "templates" / "skills"
#: The application's choice of agents and the hash of each file written for
#: them, relative to the project root (committed: the team shares it).
RECORD = ".sandloom/agent-files.json"
#: A pointer file for an agent that reads its own instruction file name.
_POINTER = "@AGENTS.md\n"


@dataclass(frozen=True)
class CodingAgent:
    """A coding agent: where it reads skills, and how to tell it is installed."""

    key: str
    name: str
    skills_dir: str
    #: An instruction file it reads instead of ``AGENTS.md`` (a one-line pointer).
    pointer: str | None
    executables: tuple[str, ...]
    homes: tuple[str, ...]


#: Folders and files from each vendor's documentation (checked 2026-11-02).
CODING_AGENTS: tuple[CodingAgent, ...] = (
    CodingAgent(
        "claude",
        "Claude Code",
        ".claude/skills",
        "CLAUDE.md",
        ("claude",),
        ("~/.claude",),
    ),
    CodingAgent("codex", "Codex", ".agents/skills", None, ("codex",), ("~/.codex",)),
    CodingAgent(
        "gemini",
        "Gemini CLI / Antigravity",
        ".agents/skills",
        "GEMINI.md",
        ("gemini", "agy"),
        ("~/.gemini",),
    ),
    CodingAgent(
        "cursor",
        "Cursor",
        ".agents/skills",
        None,
        ("cursor-agent", "cursor"),
        ("~/.cursor",),
    ),
    CodingAgent(
        "copilot",
        "GitHub Copilot",
        ".agents/skills",
        None,
        ("copilot",),
        ("~/.copilot",),
    ),
    CodingAgent(
        "opencode",
        "OpenCode",
        ".agents/skills",
        None,
        ("opencode",),
        ("~/.config/opencode",),
    ),
    CodingAgent("amp", "Amp", ".agents/skills", None, ("amp",), ("~/.config/amp",)),
    CodingAgent(
        "devin",
        "Devin Desktop (Windsurf)",
        ".agents/skills",
        None,
        ("devin-desktop", "windsurf"),
        ("~/.devin", "~/.codeium/windsurf"),
    ),
    CodingAgent("kiro", "Kiro", ".kiro/skills", None, ("kiro-cli",), ("~/.kiro",)),
)
#: The agents of an application that chose none: the two folders every
#: documented agent but Kiro reads.
DEFAULT_AGENTS: tuple[str, ...] = ("claude", "codex")
_BY_KEY = {a.key: a for a in CODING_AGENTS}


def agents_by_key(keys: Iterable[str]) -> list[CodingAgent]:
    """The agents named by ``keys``, in :data:`CODING_AGENTS` order."""
    wanted = set(keys)
    unknown = sorted(wanted - set(_BY_KEY))
    if unknown:
        known = ", ".join(_BY_KEY)
        raise ValueError(f"unknown agent(s) {', '.join(unknown)}; choose from {known}")
    return [a for a in CODING_AGENTS if a.key in wanted]


def parse_agents(value: str) -> list[str]:
    """``"claude, codex"`` -> ``["claude", "codex"]`` (validated)."""
    keys = [k.strip().lower() for k in value.split(",") if k.strip()]
    return [a.key for a in agents_by_key(keys)]


def detect_agents(
    which: Callable[[str], str | None] = shutil.which, home: Path | None = None
) -> dict[str, str]:
    """``{agent key: what showed it}`` for each agent installed on this machine:
    an executable on ``PATH`` or its user folder."""
    base = home if home is not None else Path.home()
    found: dict[str, str] = {}
    for agent in CODING_AGENTS:
        exe = next((e for e in agent.executables if which(e)), None)
        if exe is not None:
            found[agent.key] = exe
            continue
        for folder in agent.homes:
            if (base / folder.removeprefix("~/")).is_dir():
                found[agent.key] = folder
                break
    return found


def skill_dirs(agents: Sequence[str]) -> list[str]:
    """The skill folders the agents read, each once, in agent order."""
    return list(dict.fromkeys(a.skills_dir for a in agents_by_key(agents)))


def skill_files(app: str, agents: Sequence[str] = DEFAULT_AGENTS) -> dict[str, str]:
    """``{path relative to the project root: content}`` of every app skill for
    ``agents``, with ``<app>`` filled in, and each agent's pointer file."""
    files: dict[str, str] = {}
    for name in APP_SKILLS:
        text = (_TEMPLATES / name / "SKILL.md").read_text(encoding="utf-8")
        text = text.replace("<app>", app)
        for directory in skill_dirs(agents):
            files[f"{directory}/{name}/SKILL.md"] = text
    for agent in agents_by_key(agents):
        if agent.pointer:
            files[agent.pointer] = _POINTER
    return files


def contract_files(modules: str, agents: Sequence[str]) -> dict[str, str]:
    """The module-author contract for every module under ``modules`` (a path
    relative to the project root), with each agent's pointer file beside it."""
    files = {f"{modules}/AGENTS.md": _wiring.module_contract()}
    for agent in agents_by_key(agents):
        if agent.pointer:
            files[f"{modules}/{agent.pointer}"] = _POINTER
    return files


def _digest(text: str) -> str:
    """The SHA-256 hex digest the record keeps for a file's content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_record(root: Path) -> tuple[list[str] | None, dict[str, str]]:
    """``(agents, {path: hash})`` from :data:`RECORD`; ``(None, {})`` without one."""
    path = root / RECORD
    if not path.is_file():
        return None, {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("agents", [])), dict(data.get("files", {}))


def save_record(root: Path, agents: Sequence[str], hashes: dict[str, str]) -> Path:
    """Write :data:`RECORD`: the agents in catalogue order and each file's hash."""
    path = root / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = [a.key for a in agents_by_key(agents)]
    data = {"agents": keys, "files": dict(sorted(hashes.items()))}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def write_managed(
    root: Path, files: dict[str, str], agents: Sequence[str], *, force: bool = False
) -> tuple[list[Path], list[Path]]:
    """Write ``files`` under ``root``; returns ``(written, kept)``.

    A missing file is written. A file the framework wrote earlier (its hash is
    in the record) is replaced with the new content. A file that differs from
    both is yours: it is kept unless ``force``. The record is rewritten with
    ``agents`` and the hash of every file now matching the framework's."""
    _, recorded = load_record(root)
    hashes = dict(recorded)
    written: list[Path] = []
    kept: list[Path] = []
    for rel, content in files.items():
        path = root / rel
        if path.is_file():
            current = path.read_text(encoding="utf-8")
            if current == content:
                hashes[rel] = _digest(content)
                continue
            if recorded.get(rel) != _digest(current) and not force:
                kept.append(path)
                continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        hashes[rel] = _digest(content)
        written.append(path)
    save_record(root, agents, hashes)
    return written, kept


def write_skills(
    root: Path,
    app: str,
    agents: Sequence[str] = DEFAULT_AGENTS,
    *,
    modules: str | None = None,
    force: bool = False,
) -> tuple[list[Path], list[Path]]:
    """Write the app skills for ``agents`` under ``root``, and the module-author
    contract under ``modules`` when given; see :func:`write_managed`."""
    files = skill_files(app, agents)
    if modules is not None:
        files.update(contract_files(modules, agents))
    return write_managed(root, files, agents, force=force)


def ask_agents(
    detected: dict[str, str],
    preselected: Sequence[str],
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> list[str]:
    """Ask which agents to install for; Enter keeps ``preselected``."""
    write("Coding agents for this project (each reads skills from its own folder):")
    for n, agent in enumerate(CODING_AGENTS, 1):
        mark = "x" if agent.key in preselected else " "
        seen = f"  (found: {detected[agent.key]})" if agent.key in detected else ""
        write(f"  [{mark}] {n}. {agent.name:<26} {agent.skills_dir}{seen}")
    while True:
        answer = read("Numbers or names, comma-separated (Enter keeps the marked): ")
        if not answer.strip():
            return list(preselected)
        try:
            return _choice(answer)
        except ValueError as exc:
            write(str(exc))


def _choice(answer: str) -> list[str]:
    """Agent keys from an answer of numbers and names; raises on an unknown one."""
    keys: list[str] = []
    for part in (p.strip().lower() for p in answer.split(",") if p.strip()):
        if part.isdigit() and 1 <= int(part) <= len(CODING_AGENTS):
            keys.append(CODING_AGENTS[int(part) - 1].key)
        else:
            keys.append(part)
    return [a.key for a in agents_by_key(keys)]


def choose_agents(
    root: Path | None,
    given: str | None,
    *,
    interactive: bool,
    reask: bool = False,
    detect: Callable[[], dict[str, str]] = detect_agents,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> list[str]:
    """The agents to install for: ``given`` (``--agents``), else the record of
    ``root``, else the installed ones (asked when ``interactive``), else
    :data:`DEFAULT_AGENTS`. ``reask`` asks even when the record has a choice."""
    if given:
        return parse_agents(given)
    recorded = load_record(root)[0] if root is not None else None
    if recorded and not (reask and interactive):
        return [a.key for a in agents_by_key(recorded)]
    detected = detect()
    preselected = recorded or [a.key for a in CODING_AGENTS if a.key in detected]
    preselected = preselected or list(DEFAULT_AGENTS)
    if interactive:
        return ask_agents(detected, preselected, read, write)
    return preselected


def stdin_is_interactive() -> bool:
    """A person at a terminal (not a pipe, not CI, not an agent's shell)."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


__all__ = [
    "APP_SKILLS",
    "CODING_AGENTS",
    "DEFAULT_AGENTS",
    "RECORD",
    "CodingAgent",
    "agents_by_key",
    "ask_agents",
    "choose_agents",
    "contract_files",
    "detect_agents",
    "load_record",
    "parse_agents",
    "save_record",
    "skill_dirs",
    "skill_files",
    "stdin_is_interactive",
    "write_managed",
    "write_skills",
]
