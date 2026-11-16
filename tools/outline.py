#!/usr/bin/env python3
"""What is inside a file, without reading it whole: the fleet agents' outline.

    python3 tools/outline.py PATH [PATH ...]     the structure, with line ranges
    python3 tools/outline.py PATH::NAME [...]    one symbol or section

``PATH`` is a Python file (classes, functions and methods with their
signatures, module constants), a Markdown file (headings), or a directory (the
top level of each Python file in it). Every entry carries its line range
``START-END`` (inclusive): ``Read`` it with ``offset=START`` and
``limit=END-START+1``.

``PATH::NAME`` narrows to one entry. ``NAME`` is ``Class``, ``Class.method``,
``function`` or, in Markdown, any part of a heading:

- a class prints its own outline (members, no bodies);
- a function, a method or a heading prints its source with line numbers.

A name that matches nothing lists the closest names; output past
``MAX_LINES`` is cut with a note saying how to narrow it. Reads only.
"""

from __future__ import annotations

import ast
import difflib
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

MAX_LINES = 250
SIGNATURE_WIDTH = 140
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
#: What reading or parsing one file can raise: an unreadable or missing file, a
#: directory named as a file, bad syntax or encoding, an expression too deep for
#: ``ast``. Each becomes an ``error:`` line naming the file.
FAILURES = (LookupError, ValueError, SyntaxError, OSError, MemoryError, RecursionError)
#: Two characters or more, so a ``T = TypeVar("T")`` is not one.
_CONSTANT = re.compile(r"^_*[A-Z][A-Z0-9_]+$")


@dataclass
class Entry:
    """One line of an outline: a class, a function, a constant or a heading."""

    name: str
    label: str
    start: int
    end: int
    depth: int
    kind: str
    children: list[Entry] = field(default_factory=list)

    def line(self) -> str:
        span = f"{self.start}" if self.start == self.end else f"{self.start}-{self.end}"
        return f"{span:>11}  {'  ' * self.depth}{self.label}"

    def walk(self) -> Iterator[Entry]:
        yield self
        for child in self.children:
            yield from child.walk()


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SIGNATURE_WIDTH else text[: SIGNATURE_WIDTH - 1] + "…"


def _decorators(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    names = []
    for d in node.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        names.append("@" + ast.unparse(target))
    return " ".join(names) + " " if names else ""


def _start(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    return min([node.lineno, *(d.lineno for d in node.decorator_list)])


def _function(
    node: ast.FunctionDef | ast.AsyncFunctionDef, depth: int, owner: str
) -> Entry:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    signature = f"{prefix} {node.name}({ast.unparse(node.args)}){returns}"
    name = f"{owner}.{node.name}" if owner else node.name
    end = node.end_lineno or node.lineno
    return Entry(
        name, _clip(_decorators(node) + signature), _start(node), end, depth, "def"
    )


def _class(node: ast.ClassDef, depth: int, owner: str) -> Entry:
    bases = ", ".join(ast.unparse(b) for b in [*node.bases, *node.keywords])
    name = f"{owner}.{node.name}" if owner else node.name
    head = f"class {node.name}" + (f"({bases})" if bases else "")
    end = node.end_lineno or node.lineno
    entry = Entry(
        name, _clip(_decorators(node) + head), _start(node), end, depth, "class"
    )
    entry.children = _body(node.body, depth + 1, name)
    return entry


def _body(body: list[ast.stmt], depth: int, owner: str) -> list[Entry]:
    out: list[Entry] = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            out.append(_class(node, depth, owner))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out.append(_function(node, depth, owner))
        elif isinstance(node, ast.If | ast.Try | ast.TryStar | ast.With):
            # `if TYPE_CHECKING:`, `try: import x except ImportError:` and the
            # like define names at the level they sit in.
            for block in _blocks(node):
                out += _body(block, depth, owner)
        elif not owner and isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and (
                    _CONSTANT.match(t.id) or t.id == "__all__"
                ):
                    end = node.end_lineno or node.lineno
                    out.append(Entry(t.id, t.id, node.lineno, end, depth, "constant"))
    return out


def _blocks(node: ast.If | ast.Try | ast.TryStar | ast.With) -> list[list[ast.stmt]]:
    if isinstance(node, ast.If):
        return [node.body, node.orelse]
    if isinstance(node, ast.With):
        return [node.body]
    handlers = [h.body for h in node.handlers]
    return [node.body, *handlers, node.orelse, node.finalbody]


def python_outline(text: str, filename: str = "<file>") -> list[Entry]:
    """Classes, functions and methods (nested functions left out), and the
    module's constants."""
    return _body(ast.parse(text, filename=filename).body, 0, "")


def markdown_outline(text: str) -> list[Entry]:
    """Headings outside fenced code, each running to the next heading of the
    same or a higher level."""
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        m = _FENCE.match(line)
        if m:
            mark = m.group(1)[0]
            if fence is None:
                fence = mark
            elif fence == mark:
                fence = None
            continue
        if fence is None and (h := _HEADING.match(line)):
            found.append((number, len(h.group(1)), h.group(2)))
    entries: list[Entry] = []
    open_: list[Entry] = []  # headings whose section has not ended yet
    for number, level, title in found:
        while open_ and open_[-1].depth >= level - 1:
            open_.pop().end = number - 1
        entry = Entry(title, f"{'#' * level} {title}", number, 0, level - 1, "heading")
        entries.append(entry)
        open_.append(entry)
    for entry in open_:
        entry.end = len(lines)
    return entries


def outline(path: Path) -> list[Entry]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        return python_outline(text, str(path))
    if path.suffix in (".md", ".markdown"):
        return markdown_outline(text)
    kind = path.suffix or path.name
    raise ValueError(f"{path}: no outline for '{kind}' files; grep -n instead")


def _flat(entries: list[Entry]) -> list[Entry]:
    return [e for top in entries for e in top.walk()]


def render_file(path: Path, top_only: bool = False) -> list[str]:
    entries = outline(path)
    total = len(path.read_text(encoding="utf-8").splitlines())
    shown = entries if top_only else _flat(entries)
    return [f"{path}  ({total} lines)", *(e.line() for e in shown)]


def render_symbol(path: Path, name: str) -> list[str]:
    entries = _flat(outline(path))
    if path.suffix == ".py":
        hits = [e for e in entries if e.name == name] or [
            e for e in entries if e.name.split(".")[-1] == name
        ]
    else:
        hits = [e for e in entries if name.lower() in e.name.lower()]
    if not hits:
        names = [e.name for e in entries]
        close = difflib.get_close_matches(name, names, n=5, cutoff=0.4)
        hint = f"; closest: {', '.join(close)}" if close else ""
        raise LookupError(f"{path}: nothing named {name!r}{hint}")
    if len(hits) > 1:
        head = f"{path}::{name} matches {len(hits)}; name one:"
        return [head, *(e.line() for e in hits)]
    hit = hits[0]
    if hit.kind == "class":
        return [f"{path}::{hit.name}", *(e.line() for e in hit.walk())]
    lines = path.read_text(encoding="utf-8").splitlines()
    body = [f"{n:>6}  {lines[n - 1]}" for n in range(hit.start, hit.end + 1)]
    return [f"{path}::{hit.name}  (lines {hit.start}-{hit.end})", *body]


def render(arg: str) -> list[str]:
    raw, _, name = arg.partition("::")
    path = Path(raw)
    if not path.exists():
        raise LookupError(f"{raw}: no such file or directory")
    if path.is_dir():
        if name:
            raise LookupError(f"{raw}: a directory has no symbols; name a file")
        out: list[str] = []
        files = sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)
        for child in files:
            try:
                out += render_file(child, top_only=True)
            except FAILURES as exc:
                # One file that cannot be read leaves the others' outline.
                out.append(_error(child, exc))
        return out or [f"{path}: no Python files"]
    try:
        return render_symbol(path, name) if name else render_file(path)
    except FAILURES as exc:
        raise _Failed(_error(path, exc)) from exc


class _Failed(Exception):
    """A failure already rendered as its ``error:`` line."""


def _error(path: Path, exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    if isinstance(exc, MemoryError | RecursionError):
        text = f"{type(exc).__name__}: too deeply nested to outline"
    return f"error: {text}" if text.startswith(str(path)) else f"error: {path}: {text}"


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    out: list[str] = []
    status = 0
    for arg in argv:
        try:
            lines = render(arg)
        except _Failed as exc:
            lines = [str(exc)]
        except FAILURES as exc:
            lines = [f"error: {exc}"]
        out += lines
        if any(line.startswith("error: ") for line in lines):
            status = 2
    if len(out) > MAX_LINES:
        cut = len(out) - MAX_LINES
        note = f"... {cut} more lines: narrow with PATH::Class, or Read a range above"
        out = [*out[:MAX_LINES], note]
    print("\n".join(out))
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
