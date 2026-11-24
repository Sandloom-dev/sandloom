"""Tier and skills metrics of one DX eval run, read from its transcript and diff.

``run_eval.py`` stores the numbers on each result (``Result`` carries the
:data:`METRIC_KEYS` fields) and aggregates them over repeated runs. They answer
two questions of the beginner tier (``__beginner__`` names, SANDLOOM-T009) and
of the agent skills a new project ships:

* did the agent need more than the tier to do the task? ``sandloom docs <name>``
  lookups and which names, recipes read, the advanced ``sandloom.sdk`` names in
  the final diff (names outside the tier are *tier misses*), the
  ``# sandloom: advanced`` markers, the T009 failures the gates printed to it;
* did the skills help? SKILL.md reads and the turn of the first green
  ``sandloom test``.

Everything is derived from ``transcript.jsonl`` (the agent's Bash / Read / Skill
calls and their results) and ``changes.patch`` (the final diff), so a stored run
can be re-measured without an agent: ``run_eval.py recompute --label <label>``.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

#: A Bash command that reads a file's text (the kernel-read and SKILL.md metrics).
READ_VERBS = re.compile(r"(^|[|;&]\s*|\s)(cat|sed|head|tail|less|nl|bat|awk)\s")

#: The fields these metrics add to ``Result`` (every one has a default there).
METRIC_KEYS = (
    "docs_lookups",
    "docs_names",
    "recipes_read",
    "recipe_index_views",
    "skills_read",
    "first_green_turn",
    "t009_failures",
    "advanced_markers",
    "sdk_names",
    "tier_misses",
    "test_sdk_names",
    "test_tier_misses",
)

#: ``sandloom docs <args>`` inside a Bash command (``uv run sandloom docs …`` too).
_DOCS_RE = re.compile(r"(?<![\w-])sandloom\s+docs\b([^|;&\n<>]*)")
#: ``sandloom test`` that is not a ``--help`` / ``-h`` call.
_TEST_RE = re.compile(r"(?<![\w-])sandloom\s+test\b([^|;&\n]*)")
_SKILL_FILE_RE = re.compile(r"([\w.-]+)/SKILL\.md\b")
#: The line SANDLOOM-T009 prints for one import (``static_checks.beginner_tier_problems``).
_T009_RE = re.compile(r"is not in the beginner tier.*SANDLOOM-T009")
_PASS_RE = re.compile(r"^\s+PASS\s", re.MULTILINE)
_FAIL_RE = re.compile(r"\bFAIL(?:ED)?\b")
_MARKER = "# sandloom: advanced"
_FROM_RE = re.compile(
    r"^[ \t]*from[ \t]+(sandloom\.sdk[\w.]*)[ \t]+import[ \t]*"
    r"(\([^)]*\)|[^\n(]*)([^\n]*)",
    re.MULTILINE,
)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+(sandloom\.sdk\.[\w.]+)", re.MULTILINE)


def _sdk_dir(sdk_dir: Path | None) -> Path:
    if sdk_dir is not None:
        return sdk_dir
    spec = importlib.util.find_spec("sandloom.sdk")
    if spec is None or spec.origin is None:
        raise RuntimeError("sandloom.sdk is not importable: run with `uv run`")
    return Path(spec.origin).parent


def beginner_tier(sdk_dir: Path | None = None) -> dict[str, set[str]]:
    """``{sandloom.sdk module: its __beginner__ names}`` of the installed framework
    (the same static read SANDLOOM-T009 gates with)."""
    from sandloom.core.archcheck import api10_rules

    return api10_rules.beginner_names(_sdk_dir(sdk_dir))


def after_first_tier(sdk_dir: Path | None = None) -> dict[str, dict[str, set[str]]]:
    """``{sandloom.sdk module: {file name: names}}`` of the installed framework's
    ``__after_first__`` (tier 2: free in a module's own top-level file of that
    name, as SANDLOOM-T009 reads it)."""
    from sandloom.core.archcheck import api10_rules

    return api10_rules.after_first_names(_sdk_dir(sdk_dir))


def _in_tier(
    path: str,
    module: str,
    name: str,
    beginner: dict[str, set[str]],
    after_first: dict[str, dict[str, set[str]]],
) -> bool:
    """Whether ``name`` of ``module`` imported in the diff file ``path`` is no tier
    miss: a beginner name, or a tier 2 name in the file SANDLOOM-T009 frees it in
    (``api10_rules.tier2_names_here``, the one place that rule lives)."""
    from sandloom.core.archcheck import api10_rules

    if name in beginner.get(module, set()):
        return True
    parts = Path(path).parts
    if "modules" not in parts[:-2]:
        return False
    # the module's directory is the nearest `modules/` above the file's own
    depth = max(i for i, p in enumerate(parts[:-2]) if p == "modules")
    modules_path = Path(*parts[: depth + 1])
    return name in api10_rules.tier2_names_here(
        Path(path), module, after_first, modules_path
    )


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return ""


def _blocks(transcript: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """``(turn, block)`` of every tool_use and tool_result block, in order. A turn
    is one assistant message (streamed blocks of one message id count once), the
    way ``num_turns`` counts; a tool_result carries the turn of the last message."""
    turn = 0
    last_id: object = None
    for line in transcript.read_text().splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        body = msg.get("message")
        if not isinstance(body, dict) or not isinstance(body.get("content"), list):
            continue
        if msg.get("type") == "assistant":
            ident = body.get("id")
            if ident is None or ident != last_id:
                turn += 1
            last_id = ident
        elif msg.get("type") != "user":
            continue
        for block in body["content"]:
            if isinstance(block, dict):
                yield turn, block


def _is_green(text: str) -> bool:
    """A ``sandloom test`` output that shows a pass: its own ``sandloom test: OK``
    line, or step lines (``PASS …``) with no ``FAIL`` among them. Output that a
    pipe cut down to nothing shows nothing and is not counted."""
    if "sandloom test: OK" in text:
        return True
    return bool(_PASS_RE.search(text)) and not _FAIL_RE.search(text)


def _docs_call(args: str, names: list[str], recipes: list[str]) -> tuple[int, int]:
    """Classify one ``sandloom docs`` call; return (lookups, recipe-index views)."""
    raw = [t.strip("'\"`") for t in args.split()]
    flags = [t for t in raw if t.startswith("-")]
    if "-h" in flags or "--help" in flags:
        return 0, 0  # asking for help is no lookup
    tokens = [t for t in raw if t and not t.startswith("-") and not t.isdigit()]
    if not tokens:
        return (0, 0) if flags else (1, 0)  # `docs --write` etc. is no lookup
    if tokens[0] == "recipes" or (tokens[0] == "recipe" and len(tokens) == 1):
        return 0, 1
    if tokens[0] == "recipe":
        recipes.append(tokens[1])
        return 0, 0
    names.append(tokens[0])
    return 1, 0


def transcript_metrics(transcript: Path) -> dict[str, Any]:
    """The transcript half of :data:`METRIC_KEYS` (no diff metrics)."""
    lookups = views = t009 = 0
    names: list[str] = []
    recipes: list[str] = []
    skills: set[str] = set()
    test_calls: dict[str, int] = {}  # tool_use id of a `sandloom test` -> its turn
    first_green: int | None = None
    for turn, block in _blocks(transcript):
        kind = block.get("type")
        if kind == "tool_use":
            inp = block.get("input", {})
            tool = block.get("name")
            cmd = str(inp.get("command", "")) if tool == "Bash" else ""
            if tool == "Skill":
                skills.add(str(inp.get("skill", "")))
            elif tool == "Read":
                hit = _SKILL_FILE_RE.search(str(inp.get("file_path", "")))
                if hit:
                    skills.add(hit.group(1))
            elif tool == "Bash":
                if READ_VERBS.search(cmd):
                    skills.update(_SKILL_FILE_RE.findall(cmd))
                for found in _DOCS_RE.finditer(cmd):
                    n, v = _docs_call(found.group(1), names, recipes)
                    lookups += n
                    views += v
                if any(
                    not re.search(r"(--help|\s-h\b)", m.group(1))
                    for m in _TEST_RE.finditer(cmd)
                ):
                    test_calls[str(block.get("id"))] = turn
        elif kind == "tool_result":
            text = _result_text(block.get("content"))
            t009 += len(_T009_RE.findall(text))
            at = test_calls.get(str(block.get("tool_use_id")))
            if (
                first_green is None
                and at is not None
                and not block.get("is_error")
                and _is_green(text)
            ):
                first_green = at
    return {
        "docs_lookups": lookups,
        "docs_names": sorted(set(names)),
        "recipes_read": sorted(set(recipes)),
        "recipe_index_views": views,
        "skills_read": sorted(skills - {""}),
        "first_green_turn": first_green,
        "t009_failures": t009,
    }


def _added_python(patch: str) -> list[tuple[str, str]]:
    """``(path, added text)`` of each ``.py`` file of a unified diff. A file starts
    at its ``+++ b/<path>`` header, which both ``git diff`` (framework mode) and
    ``appmode.app_patch`` (``difflib``: no ``diff --git`` line) write. Hunk
    counts tell a header from a changed line that starts with ``++``/``--``."""
    files: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    old_left = new_left = 0
    for line in patch.splitlines():
        if old_left or new_left:
            if line.startswith("+"):
                new_left -= 1
                if current is not None:
                    current.append(line[1:])
            elif line.startswith("-"):
                old_left -= 1
            elif not line.startswith("\\"):  # context
                old_left -= 1
                new_left -= 1
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            old_left, new_left = (int(n) if n else 1 for n in hunk.groups())
        elif line.startswith("+++ "):
            path = line[4:].split("\t")[0]
            current = None
            if path.endswith(".py") and path != "/dev/null":
                current = []
                files.append((path.removeprefix("b/"), current))
    return [(path, "\n".join(lines)) for path, lines in files]


def _file_kind(path: str) -> str | None:
    """``code`` for a module's own code (what SANDLOOM-T009 gates: under an
    application's ``modules/<module>/``, not generated), ``test`` for a module's
    test file, ``None`` for anything else (generated code, shared contracts,
    wiring), which the agent did not write against the tier."""
    parts = path.split("/")
    if "modules" not in parts[:-2] or "_generated" in parts:
        return None
    name = parts[-1]
    is_test = "tests" in parts[:-1] or name.startswith("test_") or name == "conftest.py"
    return "test" if is_test else "code"


def _imported(names_text: str) -> list[str]:
    lines = (re.sub(r"#.*", "", ln) for ln in names_text.strip("() \t").splitlines())
    parts = (p.strip() for ln in lines for p in ln.split(","))
    return [p.split(" as ")[0].strip() for p in parts if p and p != "\\"]


def diff_metrics(
    patch: str,
    beginner: dict[str, set[str]],
    after_first: dict[str, dict[str, set[str]]] | None = None,
) -> dict[str, Any]:
    """The diff half of :data:`METRIC_KEYS`, counted the way SANDLOOM-T009 does:
    the ``sandloom.sdk`` names the module code of the final diff imports
    (``module.Name``), those outside the beginner tier (a module the tier does not
    list has no beginner names) and the advanced markers. A tier 2 name
    (``after_first``, :func:`after_first_tier`) is no miss in the file of a
    module's own directory that the tier names for it (``sagas.py``), a miss
    anywhere else; ``None`` is a tier with no tier 2 names. Generated code and
    files outside an application's ``modules/`` are not counted; a module's test
    files are reported apart (``test_sdk_names``, ``test_tier_misses``)."""
    later = after_first or {}
    names: dict[str, set[str]] = {"code": set(), "test": set()}
    misses: dict[str, set[str]] = {"code": set(), "test": set()}
    markers = 0
    for path, text in _added_python(patch):
        kind = _file_kind(path)
        if kind is None:
            continue
        if kind == "code":
            markers += text.count(_MARKER)
        for found in _FROM_RE.finditer(text):
            module = found.group(1)
            for name in _imported(found.group(2)):
                names[kind].add(f"{module}.{name}")
                if not _in_tier(path, module, name, beginner, later):
                    misses[kind].add(f"{module}.{name}")
        for found in _IMPORT_RE.finditer(text):
            names[kind].add(found.group(1))
            misses[kind].add(found.group(1))
    return {
        "advanced_markers": markers,
        "sdk_names": sorted(names["code"]),
        "tier_misses": sorted(misses["code"]),
        "test_sdk_names": sorted(names["test"]),
        "test_tier_misses": sorted(misses["test"]),
    }


def run_metrics(
    transcript: Path,
    patch: Path,
    beginner: dict[str, set[str]],
    after_first: dict[str, dict[str, set[str]]] | None = None,
) -> dict[str, Any]:
    """:data:`METRIC_KEYS` of one run. A missing transcript raises (a metric that
    read nothing must not look like a run that did nothing); a missing patch is a
    run that stopped before it wrote one (no application): an empty diff."""
    out = transcript_metrics(transcript)
    out.update(
        diff_metrics(
            patch.read_text() if patch.is_file() else "", beginner, after_first
        )
    )
    return out
