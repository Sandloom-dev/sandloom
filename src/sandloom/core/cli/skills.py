"""The agent skills an ``sandloom new`` application ships (``sandloom add skills``).

The framework's procedures, rewritten for an application's paths and
commands (packaged under ``sandloom/core/cli/templates/skills/``): create a
module, run the gates, roll an event to ``.v2``, split into services.
``sandloom new`` writes them to ``.claude/skills/<name>/SKILL.md`` (Claude Code)
and ``.agents/skills/<name>/SKILL.md`` (other agents); ``sandloom add skills``
writes them into an existing application, keeping a file that differs
unless ``--force``.
"""

from __future__ import annotations

from pathlib import Path

#: The skills an application gets, in the order the map lists them.
APP_SKILLS: tuple[str, ...] = (
    "sandloom-new-module",
    "sandloom-quality-gates",
    "sandloom-event-version-roll",
    "sandloom-split-deploy",
)
#: Where agents look for skills, relative to the project root.
SKILL_DIRS: tuple[str, ...] = (".claude/skills", ".agents/skills")
_TEMPLATES = Path(__file__).resolve().parent / "templates" / "skills"


def skill_files(app: str) -> dict[str, str]:
    """``{path relative to the project root: content}`` of every app skill,
    with ``<app>`` filled in."""
    files: dict[str, str] = {}
    for name in APP_SKILLS:
        text = (_TEMPLATES / name / "SKILL.md").read_text(encoding="utf-8")
        text = text.replace("<app>", app)
        for directory in SKILL_DIRS:
            files[f"{directory}/{name}/SKILL.md"] = text
    return files


def write_skills(
    root: Path, app: str, *, force: bool = False
) -> tuple[list[Path], list[Path]]:
    """Write the app skills under ``root``; returns ``(written, kept)``: a
    file that exists with other content is kept unless ``force``."""
    written: list[Path] = []
    kept: list[Path] = []
    for rel, content in skill_files(app).items():
        path = root / rel
        if path.is_file():
            current = path.read_text(encoding="utf-8")
            if current == content:
                continue
            if not force:
                kept.append(path)
                continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written, kept


__all__ = ["APP_SKILLS", "SKILL_DIRS", "skill_files", "write_skills"]
