"""Register a freshly generated module where its project loads modules.

``sandloom add module`` would otherwise stop at "now add the module to
LOCAL_MODULES, .env.example and the deploy profile". :func:`wire_module`
does them:

* **framework tree** (``src/sandloom/main.py`` defines ``LOCAL_MODULES``: the
  checkout's modules no installed distribution registers): the module joins
  ``LOCAL_MODULES``, ``SANDLOOM_MODULES`` in ``.env.example`` and the monolith
  deploy profile;
* **application** (``sandloom new``): the module joins ``SANDLOOM_MODULES`` in ``.env``,
  ``.env.example``, ``.env.test`` and the deploy profile
  (``deploy/docker-compose.yml``).

Test paths and coverage need no edit: the framework's ``testpaths`` /
coverage source are ``src`` packages and an app's are its package, so a module's
``tests/`` is collected wherever it lands. Edits are idempotent (a module
already listed is left alone) and textual (comments and layout survive).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from sandloom.core.tasks.periodic import PERIODIC_HOOK, has_periodic_tasks

_LOCAL_MODULES = re.compile(
    r"^(LOCAL_MODULES(?::\s*list\[str\])?\s*=\s*)(\[[^\]]*\])", re.MULTILINE
)
_ENV_LINE = re.compile(r"^(SANDLOOM_MODULES=)(\[.*\])\s*$", re.MULTILINE)
_COMPOSE_LINE = re.compile(r"^(\s*SANDLOOM_MODULES:\s*')(\[.*\])('\s*)$", re.MULTILINE)


#: Minimal conftest for a module generated INTO the framework tree, whose own
#: suite does not load the ``sandloom.sdk.testing`` plugin (pytest honours
#: ``pytest_plugins`` only in a root conftest). The star import brings the
#: kit's fixtures, ``published`` recording included. Applications activate
#: the plugin in their pyproject ``addopts`` instead, so their modules get no
#: conftest.
FRAMEWORK_TREE_CONFTEST = '''"""Module-test fixtures (client, db, published)."""

from sandloom.sdk.testing.fixtures import *  # noqa: F403
'''


#: The module-author agent contract (AGENTS.md) the scaffold and ``sandloom new``
#: ship. ``src/sandloom/AGENTS.md`` is a byte-equal copy for the framework
#: tree (pinned by ``test_agent_context_budget.py``).
MODULE_CONTRACT_PATH = Path(__file__).parent / "templates" / "module-AGENTS.md"
#: First-line marker: an ancestor AGENTS.md carrying it already gives every
#: module below it the contract, so the scaffold does not copy it again.
MODULE_CONTRACT_MARKER = "<!-- sandloom:module-contract"


#: Exact public signatures; embedded verbatim in the framework's root AGENTS.md
#: and appended to a generated product's AGENTS.md.
API_CHEATSHEET_PATH = Path(__file__).parent / "templates" / "api-cheatsheet.md"


def module_contract() -> str:
    """The module-author contract (Markdown)."""
    return MODULE_CONTRACT_PATH.read_text(encoding="utf-8")


def api_cheatsheet() -> str:
    """The public API cheat-sheet (Markdown)."""
    return API_CHEATSHEET_PATH.read_text(encoding="utf-8")


def contract_inherited(directory: Path) -> bool:
    """True when ``directory`` or an ancestor has an AGENTS.md with the
    contract, or is a Sandloom project root (``architecture_rules.yml``) with its
    own AGENTS.md: an ``sandloom new`` app's map covers its modules."""
    for candidate in (directory, *directory.resolve().parents):
        agents = candidate / "AGENTS.md"
        if not agents.is_file():
            continue
        if (candidate / "architecture_rules.yml").is_file():
            return True
        if MODULE_CONTRACT_MARKER in agents.read_text(encoding="utf-8"):
            return True
    return False


def project_root(start: Path) -> Path | None:
    """The nearest ancestor of ``start`` holding a ``pyproject.toml``."""
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def _append(raw: str, dotted: str) -> str | None:
    """``raw`` JSON list with ``dotted`` appended (None: already listed)."""
    try:
        modules = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(modules, list) or dotted in modules:
        return None
    separator = ", " if '", "' in raw else ","
    return "[" + separator.join(json.dumps(m) for m in [*modules, dotted]) + "]"


def _rewrite(path: Path, pattern: re.Pattern[str], dotted: str) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    changed = False

    def repl(match: re.Match[str]) -> str:
        nonlocal changed
        new = _append(match.group(2), dotted)
        if new is None:
            return match.group(0)
        changed = True
        tail = match.group(3) if match.lastindex and match.lastindex >= 3 else ""
        return match.group(1) + new + tail

    updated = pattern.sub(repl, text)
    if changed:
        path.write_text(updated, encoding="utf-8")
    return changed


def _python_list_append(path: Path, dotted: str) -> bool:
    """``LOCAL_MODULES = [...]`` in the framework's composition root."""
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    match = _LOCAL_MODULES.search(text)
    if match is None or f'"{dotted}"' in match.group(2):
        return False
    body = match.group(2)[1:-1].rstrip().rstrip(",")
    new = f'[{body}, "{dotted}"]' if body.strip() else f'["{dotted}"]'
    path.write_text(
        text[: match.start(2)] + new + text[match.end(2) :], encoding="utf-8"
    )
    return True


def wire_module(dotted: str, module_dir: Path) -> list[Path]:
    """Add ``dotted`` to the project's module lists; returns the edited files."""
    root = project_root(module_dir.parent)
    if root is None:
        return []
    edited: list[Path] = []
    main = root / "src" / "sandloom" / "main.py"
    if _python_list_append(main, dotted):
        edited.append(main)
    edited += [
        root / name
        for name in (".env.example", ".env", ".env.test")
        if _rewrite(root / name, _ENV_LINE, dotted)
    ]
    # The app's deploy profile, and the framework repository's monolith
    # profile (deploy/ next to backend/). The microservices profile is a
    # topology choice: `sandloom add deploy` (sandloom.core.cli.scaffold_deploy).
    for compose in (
        root / "deploy" / "docker-compose.yml",
        root / "deploy" / "compose" / "monolith.yml",
        root.parent / "deploy" / "compose/monolith.yml",
    ):
        if _rewrite(compose, _COMPOSE_LINE, dotted):
            edited.append(compose)
    return edited


__all__ = [
    "API_CHEATSHEET_PATH",
    "FRAMEWORK_TREE_CONFTEST",
    "MODULE_CONTRACT_MARKER",
    "MODULE_CONTRACT_PATH",
    "api_cheatsheet",
    "contract_inherited",
    "module_contract",
    "PERIODIC_HOOK",
    "has_periodic_tasks",
    "project_root",
    "wire_module",
]
