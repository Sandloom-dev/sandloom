#!/usr/bin/env python3
"""Fail on the mechanical style rules of the docs-writing skill, in the prose docs.

Rules (ids match ``.claude/skills/sandloom-docs-writing/style.md``, Style and Code,
and ``tells.md`` for the P rules):

- **S4**  a prose sentence over 26 words. Code, tables, front matter, headings,
  admonition and tab titles, HTML and link targets are not prose.
- **S8**  simply, just, easy, easily, quick, obviously, robust, powerful in prose
  (headings, titles and table cells included). Inline code is not prose. The
  phrases in ``S8_ALLOWED`` stay: "easy to get wrong", outcomes such as "quick
  to change".
- **S9**  the time words currently, soon, and "now" / "latest" where a pattern
  in ``S9_PATTERNS`` says they mean time ("is now", "now supports", "for now",
  "the latest version"). Changelog and upgrade pages are exempt. Misses: "new"
  is never reported (an ordinary adjective too often), and "now" / "latest" in
  a phrasing the patterns do not list ("Now, Sandloom ...", "latest release
  notes" without "the").
- **S11** a heading over 70 characters, or Title Case on a heading of four or
  more words: at least two words after the first (or after a colon) that are
  neither small words, identifiers, acronyms nor in ``PROPER_NOUNS``, and all
  of them capitalised.
- **S13** a bullet list of more than 6 items at one level. Exempt: reference
  pages (``docs/reference/**`` or front matter ``type: reference``) and a list
  the page marks as lookup with ``<!-- lookup -->`` on the line before it.
- **P6**  a paragraph with two or more series of three ("a, b and c"): forced
  triads (``tells.md``).
- **P7**  three sentences in a row, in one paragraph, opening on the same word.
- **P8**  a paragraph with two or more ", so" clauses: a chain of consequences.
- **P9**  an em dash, or a spaced en dash, in prose.
- **P12** the model words in ``P12_WORDS`` in prose.
- **W1**  an entry page (front matter ``type: entry``) over ``ENTRY_MAX_WORDS``
  words of prose: body text, list items and card lines; headings, titles,
  card titles (a line that is one bold link), tables and code do not count.
- **K3**  a fenced code block with no prose sentence directly before it: the
  previous line (blank lines and HTML comments skipped) is a heading, a tab
  title, or text ending in ``.``, ``:``, ``?`` or ``!`` (a list item ending in
  ":" included).

Scanned: tracked ``docs/**/*.md`` except ``docs/adr/**``, ``docs/design/**``,
``docs/archive/**`` and generated pages (``GENERATED_PREFIXES``, and a page
whose first line after the front matter is a ``<!-- Generated ... -->`` header).

Waiver: ``<!-- prose-ok: <rule>[,<rule>] <reason> -->`` on the reported line or
the line before it. The reason is required.

Ratchet: the docs predate these rules, so ``docs_prose_lint_baseline.json``
holds the number of hits per (file, rule). The check fails when a count rises
above its baseline or a file without an entry has hits, and reports a count
that dropped. ``--update-baseline`` only lowers counts and drops zeros; it never
raises one. ``--reset-baseline`` rewrites it from the current hits, for a
reviewed change of the rules only.

Usage::

    python3 tools/docs/docs_prose_lint.py                    # ratchet check
    python3 tools/docs/docs_prose_lint.py PATH ...           # strict, no baseline
    python3 tools/docs/docs_prose_lint.py --summary          # counts per rule and file
    python3 tools/docs/docs_prose_lint.py --update-baseline  # lower counts that dropped
    python3 tools/docs/docs_prose_lint.py --self-test
    uv run python tools/docs/docs_prose_lint.py --hook       # a Claude Code hook

``--hook`` reads a PostToolUse event on stdin, and when the edited file is a
page in scope runs this lint and ``docs_code_check.py`` on it, strictly. Hits go
to stderr with exit 2, so the agent sees them in the same turn; anything else
exits 0. The sandloom-docs-writing skill registers it in its frontmatter.

Standard library only; exit 1 on a hit (strict) or a count above its baseline.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).resolve().with_name("docs_prose_lint_baseline.json")

MAX_WORDS = 26
ENTRY_MAX_WORDS = 550
MAX_HEADING_CHARS = 70
MAX_LIST_ITEMS = 6

EXEMPT_PREFIXES = ("docs/adr/", "docs/design/", "docs/archive/")
# Pages a generator writes; their prose is fixed in the generator's source.
GENERATED_PREFIXES = (
    "docs/snippets/generated/",  # tools/docs/docs_generated.py (SNIPPETS)
    "docs/reference/sdk/",  # tools/docs/docs_reference.py
    "docs/reference/error-codes.md",  # tools/docs/docs_reference.py
    "docs/reference/configuration.md",  # tools/docs/gen_config_reference.py
    "docs/reference/configuration-tiers.md",  # tools/docs/gen_config_reference.py
    "docs/reference/routes.md",  # tools/docs/gen_reference_pages.py
    "docs/reference/guarantees.md",  # tools/checks/guarantees.py render
)
_GENERATED_HEADER = re.compile(r"^\s*<!--\s*generated\b", re.IGNORECASE)

S8_WORDS = re.compile(
    r"\b(simply|just|easy|easily|quick|obviously|robust|powerful)\b", re.IGNORECASE
)
# Phrases that keep an S8 word: a warning, or an outcome the page backs.
S8_ALLOWED = (
    "easy to get wrong",
    "easy to miss",
    "easy to forget",
    "easy to break",
    "quick to change",
    "quick to read",
    "quick to review",
    "quick start",
    "just-in-time",
)
_S8_ALLOWED = tuple(re.compile(re.escape(p), re.IGNORECASE) for p in S8_ALLOWED)

S9_PATTERNS: tuple[str, ...] = (
    r"\bcurrently\b",
    r"(?<!as )\bsoon\b",
    r"\b(?:is|are|was|were|has)\s+now\b",
    # Third person only: "Now run the tests" is a step, not a date.
    r"\bnow\s+(?:supports|has|includes|uses|ships|offers|accepts|returns"
    r"|requires|lets|provides|available|works|runs|comes)\b",
    r"\b(?:for|until|till|right|as of|by|from)\s+now\b",
    r"\b(?:the|our)\s+latest\s+(?:version|release|changes?|features?|updates?|"
    r"sandloom)\b",
    r"\blatest\s+(?:version|release)s?\b",
)
_S9 = tuple(re.compile(p, re.IGNORECASE) for p in S9_PATTERNS)
S9_EXEMPT_NAMES = ("changelog", "upgrade")

P12_WORDS = re.compile(
    r"\b(delve[sd]?|crucial|pivotal|seamless(?:ly)?|leverag(?:e|es|ed|ing)|unlock(?:s|ed)?"
    r"|empower(?:s|ed)?|supercharg(?:e|es|ed)|elevat(?:e|es|ed)|journey|landscape"
    r"|showcas(?:e|es|ed|ing)|underscor(?:e|es|ed)|testament|vibrant"
    r"|meticulous(?:ly)?|additionally)\b",
    re.IGNORECASE,
)
_SO_CLAUSE = re.compile(r",\s+so\b(?!\s+(?:far|much|many|long)\b)", re.IGNORECASE)
_TRIAD = re.compile(r"[^,;]+,\s+[^,;]+,?\s+(?:and|or)\s+\S")
# A card title: an optional icon and one bold link, alone on its line.
_CARD_TITLE = re.compile(r"^(:[\w-]+:\s*)?\*\*\[[^\]]+\]\([^)]*\)\*\*$")
_DASH = re.compile(r"\u2014|\s\u2013\s")

SMALL_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "nor",
        "for",
        "of",
        "in",
        "on",
        "at",
        "to",
        "by",
        "with",
        "from",
        "as",
        "via",
        "vs",
        "into",
        "onto",
        "per",
        "than",
        "then",
        "over",
        "off",
        "up",
        "out",
        "its",
        "it",
        "is",
        "be",
    ]
)
PROPER_NOUNS = frozenset(
    [
        "Sandloom",
        "PostgreSQL",
        "Postgres",
        "Redis",
        "Docker",
        "Compose",
        "Python",
        "FastAPI",
        "Django",
        "SQLAlchemy",
        "Alembic",
        "Pydantic",
        "Celery",
        "Stripe",
        "GitHub",
        "Kubernetes",
        "MkDocs",
        "Zensical",
        "Claude",
        "Codex",
        "Cursor",
        "OpenAPI",
        "Prometheus",
        "Grafana",
        "OpenTelemetry",
        "Linux",
        "Windows",
        "React",
        "TypeScript",
        "Markdown",
        "Public",
        "Uvicorn",
        "Gunicorn",
        "Nginx",
        "Traefik",
        "Caddy",
    ]
)

_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE = re.compile(r"^\s*\|")
_TITLE = re.compile(r"^\s*(!!!|\?\?\?\+?|===)\s*(.*)$")
# A line that opens with a block-level tag; inline tags (<a>, <code>) are prose.
_HTML_LINE = re.compile(
    r"^\s*</?(?!(?:a|abbr|b|code|em|i|kbd|span|strong|sub|sup)\b)[A-Za-z][\w-]*[\s>/]"
)
_REF_DEF = re.compile(r"^\s*(\[[^\]]+\]:\s|\*\[[^\]]+\]:)")
_RULE_LINE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LOOKUP = re.compile(r"<!--\s*lookup\b")
_WAIVER = re.compile(
    r"<!--\s*prose-ok:\s*([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*)\s+(\S[^>]*?)\s*-->"
)
_ABBREVIATIONS = re.compile(r"\b(e\.g|i\.e|etc|vs|cf|approx)\.", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?][\"')\]*_]*\s+(?=[^a-z\s])")


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def _keep_lines(repl: str | Callable[[re.Match[str]], str]) -> Callable[..., str]:
    """A re.sub replacement that keeps the newlines it removes, so text
    cleaned as one paragraph still splits back into its source lines."""

    def sub(m: re.Match[str]) -> str:
        new = repl(m) if callable(repl) else m.expand(repl)
        return new + "\n" * m.group(0).count("\n")

    return sub


_CLEAN: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (re.compile(r"<!--.*?-->", re.S), " "),
    (re.compile(r"<code>.*?</code>", re.S), "CODE"),
    (re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.S), "CODE"),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)"), r"\1"),
    (re.compile(r"\[([^\]]*)\]\[[^\]]*\]"), r"\1"),
    (re.compile(r"\[\^[^\]]+\]"), ""),
    (re.compile(r"<https?://[^>]+>"), "URL"),
    (re.compile(r"https?://\S+"), "URL"),
    (re.compile(r"</?[A-Za-z][^>]*>"), ""),
    (re.compile(r"\{\s*:?\s*[.#][^}]*\}"), ""),
    (re.compile(r":[a-z0-9]+(?:-[a-z0-9]+)+:"), ""),
    (re.compile(r"\*\*|__"), ""),
    (re.compile(r"(?<![\w*])\*(?=\S)|(?<=\S)\*(?![\w*])"), ""),
)


def clean(text: str) -> str:
    """Markdown to the prose a reader sees: code -> CODE, link -> its text,
    no HTML, no emphasis markers. Newlines are kept."""
    for rx, repl in _CLEAN:
        text = rx.sub(_keep_lines(repl), text)
    return text


def _words(text: str) -> int:
    return sum(1 for w in text.split() if any(c.isalnum() for c in w))


def _sentences(text: str) -> list[tuple[int, str]]:
    """(offset, sentence) pairs of cleaned paragraph text."""
    protected = _ABBREVIATIONS.sub(lambda m: m.group(1).replace(".", "_") + "_", text)
    out, start = [], 0
    for m in _SENTENCE_END.finditer(protected):
        out.append((start, text[start : m.end()]))
        start = m.end()
    if text[start:].strip():
        out.append((start, text[start:]))
    return out


@dataclass
class _List:
    indent: int
    bullet: bool
    first_line: int
    lookup: bool
    count: int = 1


@dataclass
class _Page:
    rel: str
    page_type: str = ""
    hits: list[Hit] = field(default_factory=list)
    words: int = 0

    @property
    def s9_exempt(self) -> bool:
        name = Path(self.rel).name.lower()
        return any(word in name for word in S9_EXEMPT_NAMES)

    @property
    def s13_exempt(self) -> bool:
        return self.rel.startswith("docs/reference/") or self.page_type == "reference"

    def add(self, line: int, rule: str, message: str) -> None:
        self.hits.append(Hit(self.rel, line, rule, message))


def _check_words(page: _Page, text: str, line_of: Callable[[int], int]) -> None:
    """S8 and S9 on cleaned text; line_of maps an offset to a source line."""
    for m in S8_WORDS.finditer(text):
        if any(
            a.start() <= m.start() < a.end()
            for rx in _S8_ALLOWED
            for a in rx.finditer(text)
        ):
            continue
        page.add(line_of(m.start()), "S8", f'"{m.group(0)}"')
    for m in P12_WORDS.finditer(text):
        page.add(line_of(m.start()), "P12", f'"{m.group(0)}" (model word)')
    for m in _DASH.finditer(text):
        page.add(line_of(m.start()), "P9", "dash as a joint")
    if page.s9_exempt:
        return
    seen: list[tuple[int, int]] = []
    for rx in _S9:
        for m in rx.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in seen):
                continue
            seen.append(m.span())
            page.add(line_of(m.start()), "S9", f'"{m.group(0)}" (time)')


def _check_paragraph(page: _Page, lines: list[tuple[int, str]]) -> None:
    text = clean("\n".join(t for _, t in lines))
    starts = [n for n, _ in lines]

    def line_of(offset: int) -> int:
        return starts[min(text.count("\n", 0, offset), len(starts) - 1)]

    raw = " ".join(t for _, t in lines).strip()
    if not _CARD_TITLE.match(raw):
        page.words += _words(text)
    sentences = _sentences(text)
    for offset, sentence in sentences:
        n = _words(sentence)
        if n <= MAX_WORDS:
            continue
        first = len(sentence) - len(sentence.lstrip())
        message = f"sentence of {n} words (max {MAX_WORDS})"
        page.add(line_of(offset + first), "S4", message)
    _check_words(page, text, line_of)
    _check_rhythm(page, text, sentences, line_of)


def _opener(sentence: str) -> str:
    words = re.findall(r"[A-Za-z']+", sentence)
    return words[0].lower() if words else ""


def _check_rhythm(
    page: _Page,
    text: str,
    sentences: list[tuple[int, str]],
    line_of: Callable[[int], int],
) -> None:
    """P6, P7 and P8: shapes repeated by rule across one paragraph."""
    flat = " ".join(text.split())
    triads = sum(1 for _, s in sentences if _TRIAD.search(" ".join(s.split())))
    if triads >= 2:
        page.add(line_of(0), "P6", f"{triads} series of three in one paragraph")
    run = 1
    for (_, prev), (offset, cur) in pairwise(sentences):
        same = _opener(cur) != "" and _opener(cur) == _opener(prev)
        run = run + 1 if same else 1
        if run == 3:
            page.add(line_of(offset), "P7", f'three sentences open on "{_opener(cur)}"')
    chains = len(_SO_CLAUSE.findall(flat))
    if chains >= 2:
        page.add(line_of(0), "P8", f'{chains} ", so" clauses in one paragraph')


def _visible_heading(text: str) -> str:
    text = re.sub(r"\{\s*:?\s*[#.][^}]*\}\s*$", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return text.replace("`", "").strip()


def _is_title_case(text: str) -> bool:
    words = clean(text).split()
    if len(words) < 4:
        return False
    candidates: list[str] = []
    after_colon = True
    for raw in words:
        word = raw.strip("\"'()[],.;:!?*").split("-")[0]
        first, after_colon = after_colon, raw.endswith(":")
        if first or not word or word == "CODE" or not word.isalpha():
            continue
        if word.lower() in SMALL_WORDS or word in PROPER_NOUNS:
            continue
        if (len(word) > 1 and word.isupper()) or any(c.isupper() for c in word[1:]):
            continue
        candidates.append(word)
    return len(candidates) >= 2 and all(w[0].isupper() for w in candidates)


def _check_heading(page: _Page, lineno: int, text: str) -> None:
    visible = _visible_heading(text)
    if len(visible) > MAX_HEADING_CHARS:
        limit = f"(max {MAX_HEADING_CHARS})"
        page.add(lineno, "S11", f"heading of {len(visible)} characters {limit}")
    if _is_title_case(visible):
        page.add(lineno, "S11", "heading in Title Case (use sentence case)")
    _check_words(page, clean(text), _at(lineno))


def _at(lineno: int) -> Callable[[int], int]:
    return lambda _offset: lineno


def _ends_sentence(text: str) -> bool:
    text = re.sub(r"<!--.*?-->", "", text).rstrip().rstrip("*_)\"'`")
    return text.endswith((".", ":", "?", "!"))


def _front_matter(page: _Page, lines: list[str]) -> int:
    """Read ``type:`` into the page; return the index of the first body line."""
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            for line in lines[1:i]:
                m = re.match(r"type:\s*(\S+)", line)
                if m:
                    page.page_type = m.group(1).strip("\"'")
            return i + 1
    return 0


def _waivers(lines: list[str]) -> dict[int, set[str]]:
    waivers: dict[int, set[str]] = {}
    for lineno, line in enumerate(lines, 1):
        for w in _WAIVER.finditer(line):
            rules = (r.strip() for r in w.group(1).split(","))
            waivers.setdefault(lineno, set()).update(rules)
    return waivers


class _Scanner:
    """One pass over a page's lines: paragraphs, headings, titles, tables,
    fences (K3) and the list stack (S13)."""

    def __init__(self, page: _Page) -> None:
        self.page = page
        self.para: list[tuple[int, str]] = []
        self.lists: list[_List] = []
        self.fence: tuple[str, int] | None = None
        self.in_comment = False
        self.prev_kind, self.prev_text = "", ""  # last significant line, for K3
        self.prev_blank = True
        self.pending_lookup = False

    def flush(self) -> None:
        if self.para:
            _check_paragraph(self.page, self.para)
        self.para = []

    def close_lists(self, indent: int) -> None:
        while self.lists and self.lists[-1].indent >= indent:
            done = self.lists.pop()
            if (
                done.bullet
                and done.count > MAX_LIST_ITEMS
                and not done.lookup
                and not self.page.s13_exempt
            ):
                self.page.add(
                    done.first_line,
                    "S13",
                    f"bullet list of {done.count} items at one level "
                    f"(max {MAX_LIST_ITEMS}; mark a lookup list <!-- lookup -->)",
                )

    def finish(self) -> None:
        self.flush()
        self.close_lists(0)

    def line(self, lineno: int, line: str) -> None:
        stripped = line.strip()
        if self.fence is not None:
            self._in_fence(line, self.fence)
        elif self.in_comment:
            self.in_comment = "-->" not in line
        elif not stripped:
            self.flush()
            self.prev_blank = True
        elif stripped.startswith("<!--"):
            # Transparent for K3 and lists; a lookup marker tags the next list.
            self.pending_lookup = self.pending_lookup or bool(_LOOKUP.search(stripped))
            self.in_comment = "-->" not in stripped
        else:
            self._content(lineno, line, stripped)

    def _in_fence(self, line: str, fence: tuple[str, int]) -> None:
        m = _FENCE.match(line)
        if (
            m
            and m.group(2)[0] == fence[0]
            and len(m.group(2)) >= fence[1]
            and not line[m.end() :].strip()
        ):
            self.fence = None
            self.prev_kind, self.prev_text, self.prev_blank = "fence", "", False

    def _content(self, lineno: int, line: str, stripped: str) -> None:
        indent = len(line) - len(line.lstrip())
        item = _ITEM.match(line)
        heading = _HEADING.match(line) if indent < 4 else None
        if item is None and not (self.para and not self.prev_blank):
            # A line that is not a list item's lazy continuation ends the
            # lists it is not indented under.
            self.close_lists(0 if heading else indent)
        self.prev_blank = False
        fence = _FENCE.match(line)
        if fence:
            self._fence_open(lineno, fence)
        elif heading:
            self.flush()
            _check_heading(self.page, lineno, heading.group(2))
            self._seen("heading", heading.group(2))
        elif title := _TITLE.match(line):
            self.flush()
            self.prev_kind = "tab" if title.group(1) == "===" else "title"
            quoted = re.findall(r'"([^"]*)"', title.group(2))
            if quoted:
                _check_words(self.page, clean(quoted[0]), _at(lineno))
        elif _TABLE.match(line):
            self.flush()
            self._seen("table", stripped)
            cells = clean(stripped)
            if not re.fullmatch(r"[|:\-\s]+", cells):
                _check_words(self.page, cells, _at(lineno))
        elif (
            _HTML_LINE.match(line)
            or _RULE_LINE.match(line)
            or _REF_DEF.match(line)
            or stripped.startswith("--8<--")
        ):
            self.flush()
            self._seen("other", stripped)
        elif item:
            self._item(lineno, item)
        else:
            self.para.append((lineno, stripped))
            continued = self.prev_kind == "item" and len(self.para) > 1
            self._seen("item" if continued else "prose", stripped)

    def _seen(self, kind: str, text: str) -> None:
        self.prev_kind, self.prev_text = kind, text
        self.pending_lookup = False

    def _fence_open(self, lineno: int, m: re.Match[str]) -> None:
        self.flush()
        ok = self.prev_kind in ("heading", "tab") or (
            self.prev_kind in ("prose", "item") and _ends_sentence(self.prev_text)
        )
        if not ok:
            self.page.add(lineno, "K3", "code block with no sentence before it")
        self.fence = (m.group(2)[0], len(m.group(2)))

    def _item(self, lineno: int, item: re.Match[str]) -> None:
        self.flush()
        indent, bullet = len(item.group(1)), item.group(2) in "-*+"
        lists = self.lists
        if lists and lists[-1].indent > indent:
            self.close_lists(indent + 1)
        if lists and lists[-1].indent == indent and lists[-1].bullet == bullet:
            lists[-1].count += 1
        else:
            if lists and lists[-1].indent == indent:
                self.close_lists(indent)
            lookup = self.pending_lookup or (bool(lists) and lists[-1].lookup)
            lists.append(_List(indent, bullet, lineno, lookup))
        self.para = [(lineno, item.group(3))]
        self._seen("item", item.group(3))


def lint_text(rel: str, text: str) -> list[Hit]:
    """Every hit in one page, waived ones removed."""
    page = _Page(rel)
    lines = text.splitlines()
    scanner = _Scanner(page)
    for idx in range(_front_matter(page, lines), len(lines)):
        scanner.line(idx + 1, lines[idx])
    scanner.finish()
    if page.page_type == "entry" and page.words > ENTRY_MAX_WORDS:
        message = f"entry page of {page.words} prose words (max {ENTRY_MAX_WORDS})"
        page.add(1, "W1", message)
    waivers = _waivers(lines)
    return [
        h
        for h in page.hits
        if h.rule not in waivers.get(h.line, set()) | waivers.get(h.line - 1, set())
    ]


def in_scope(rel: str, text: str | None = None) -> bool:
    """Whether a repository-relative POSIX path is a page this lint covers."""
    if not rel.startswith("docs/") or not rel.endswith(".md"):
        return False
    if rel.startswith(EXEMPT_PREFIXES + GENERATED_PREFIXES):
        return False
    if text is None:
        return True
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        start = next(
            (i + 1 for i in range(1, len(lines)) if lines[i].strip() == "---"), 0
        )
    first = next((ln for ln in lines[start:] if ln.strip()), "")
    return not _GENERATED_HEADER.match(first)


def tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "docs"],  # noqa: S607 - git from PATH, as CI has it
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return [f for f in out.split("\0") if f]
    except (OSError, subprocess.CalledProcessError):
        return [
            p.relative_to(root).as_posix()
            for p in sorted((root / "docs").rglob("*.md"))
        ]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def scan(root: Path, rels: Iterable[str]) -> list[Hit]:
    """Hits in the in-scope pages among ``rels``."""
    hits: list[Hit] = []
    for rel in rels:
        text = _read(root / rel)
        if text is not None and in_scope(rel, text):
            hits.extend(lint_text(rel, text))
    return hits


def scan_paths(root: Path, args: list[str]) -> list[Hit]:
    """Strict mode: a named file is linted whatever its scope (a draft may be
    untracked); a named directory contributes its in-scope pages."""
    hits: list[Hit] = []
    for arg in args:
        path = Path(arg).resolve()
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        if path.is_file():
            text = _read(path)
            if text is not None:
                hits.extend(lint_text(rel, text))
        elif path.is_dir():
            rels = [
                p.relative_to(root).as_posix()
                for p in sorted(path.rglob("*.md"))
                if root in p.parents
            ]
            hits.extend(scan(root, rels))
        else:
            print(f"{arg}: no such file or directory", file=sys.stderr)
    return hits


def counts(hits: Iterable[Hit]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for h in hits:
        rules = out.setdefault(h.path, {})
        rules[h.rule] = rules.get(h.rule, 0) + 1
    return {p: dict(sorted(r.items())) for p, r in sorted(out.items())}


def load_baseline(path: Path) -> dict[str, dict[str, int]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {p: dict(r) for p, r in data.get("counts", {}).items()}


def write_baseline(path: Path, table: dict[str, dict[str, int]]) -> None:
    table = {p: {r: n for r, n in rules.items() if n} for p, rules in table.items()}
    data = {
        "schema": 1,
        "note": (
            "Hits per (file, rule) of tools/docs/docs_prose_lint.py. Entries only "
            "shrink: lower them with --update-baseline, never raise them by hand."
        ),
        "counts": {p: dict(sorted(r.items())) for p, r in sorted(table.items()) if r},
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def compare(
    current: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> tuple[list[tuple[str, str, int, int]], list[tuple[str, str, int, int]]]:
    """(over, dropped): (file, rule, now, baseline) above and below the baseline."""
    over, dropped = [], []
    for path in sorted(set(current) | set(baseline)):
        cur, base = current.get(path, {}), baseline.get(path, {})
        for rule in sorted(set(cur) | set(base)):
            n, b = cur.get(rule, 0), base.get(rule, 0)
            if n > b:
                over.append((path, rule, n, b))
            elif n < b:
                dropped.append((path, rule, n, b))
    return over, dropped


def lowered(
    current: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """The baseline with every count lowered to the current one; never raised."""
    return {
        path: {
            rule: min(b, current.get(path, {}).get(rule, 0))
            for rule, b in rules.items()
        }
        for path, rules in baseline.items()
    }


def _self_test_rules() -> int:
    long = " ".join(["word"] * 27) + "."
    cases: dict[str, list[str]] = {
        # S4
        f"{long}\n": ["S4"],
        " ".join(["word"] * 26) + ".\n": [],
        "Short one. " + " ".join(["w"] * 30) + ".\n": ["S4"],
        "We ship generators that give agents a start, project rules that tell them "
        "how to work, and checks that stop them when they break any of the rules "
        "in CI.\n": ["S4"],
        "We ship generators that give agents a start, project rules that tell them "
        "how to work and checks that stop them when they break any of the rules "
        "in CI today.\n": ["S4"],
        f"```python\n{long}\n```\n": ["K3"],
        f"| {long} |\n|---|\n": [],
        f"<div>{long}</div>\n": [],
        f'!!! note "{long}"\n': [],
        "See [" + " ".join(["a"] * 5) + "](" + "/".join(["p"] * 40) + ") now.\n": [],
        f"---\ntitle: x\ndescription: {long}\n---\nBody.\n": [],
        "Use `" + " ".join(["c"] * 40) + "` here.\n": [],
        "First line of a long sentence "
        + " ".join(["w"] * 10)
        + "\n"
        + " ".join(["w"] * 15)
        + ".\n": ["S4"],
        # S8
        "Simply run it.\n": ["S8"],
        "It is just a module.\n": ["S8"],
        "That is easy, quick and robust.\n": ["S8", "S8", "S8"],
        "Run `simply_run()` and `just`.\n": [],
        "This step is easy to get wrong.\n": [],
        "The backend stays quick to change as it grows.\n": [],
        # P6, P7, P8, P9, P12
        "It is fast, small and safe. It reads, writes and checks.\n": ["P6"],
        "It is fast, small and safe. It reads rows.\n": [],
        "It runs. It reads. It writes.\n": ["P7"],
        "It runs. It reads. They write.\n": [],
        "It fails, so you retry. It retries, so you wait.\n": ["P8"],
        "It fails, so you retry. So far it works.\n": [],
        "The unit \u2014 the transaction \u2014 commits.\n": ["P9", "P9"],
        "Pages 1\u20133 hold it.\n": [],
        "We leverage a crucial layer.\n": ["P12", "P12"],
        # W1
        "---\ntype: entry\n---\n" + "One two. Three four. " * 138 + "\n": ["W1"],
        "---\ntype: entry\n---\n" + "One two. Three four. " * 137 + "Six.\n": [],
        "---\ntype: concept\n---\n" + "One two. Three four. " * 150 + "\n": [],
        "---\ntype: entry\n---\n"
        + "One two. Three four. " * 137
        + "Six.\n\n- :material-x: **[Card title here](a.md)**\n": [],
        "Start with the quickstart.\n": [],
        "## A powerful feature\n": ["S8"],
        # S9
        "It currently retries.\n": ["S9"],
        "Support lands soon.\n": ["S9"],
        "It runs as soon as the unit commits.\n": [],
        "Sandloom now supports Redis.\n": ["S9"],
        "The flag is now gone.\n": ["S9"],
        "Now run the tests.\n": [],
        "Install the latest version.\n": ["S9"],
        'Use `start_from="latest"` to skip history.\n': [],
        "Reads from the latest offset.\n": [],
        "New to Sandloom? Start here.\n": [],
        "Add a new module.\n": [],
        # S11
        "## " + "x" * 71 + "\n": ["S11"],
        "## " + "x" * 70 + "\n": [],
        "## How The Module Runs\n": ["S11"],
        "## How the module runs\n": [],
        "## Run It\n": [],
        "## Deploy Sandloom with PostgreSQL and Redis\n": [],
        "## Tutorial: Your first feature\n": [],
        "## Configure `SANDLOOM_MODULES` per service\n": [],
        "## The JWT and RPC keys\n": [],
        # S13
        "Items:\n\n" + "".join(f"- item {i}\n" for i in range(7)): ["S13"],
        "Items:\n\n" + "".join(f"- item {i}\n" for i in range(6)): [],
        "Steps:\n\n" + "".join(f"{i}. step {i}\n" for i in range(1, 9)): [],
        "Items:\n\n<!-- lookup -->\n" + "".join(f"- item {i}\n" for i in range(9)): [],
        "Items:\n\n- a\n"
        + "".join(f"  - sub {i}\n" for i in range(4))
        + "- b\n- c\n": [],
        "Items:\n\n" + "".join(f"- item {i}\n\n  more {i}.\n\n" for i in range(7)): [
            "S13"
        ],
        "One:\n\n- a\n- b\n- c\n- d\n\nTwo:\n\n- e\n- f\n- g\n": [],
        # K3
        "Run it:\n\n```bash\nls\n```\n": [],
        "## Install\n\n```bash\nls\n```\n": [],
        "1. Run:\n\n    ```bash\n    ls\n    ```\n": [],
        "- `notes`\n\n    ```bash\n    ls\n    ```\n": ["K3"],
        "Text.\n\n```a\nx\n```\n\n```b\ny\n```\n": ["K3"],
        "Run it.\n<!-- prose-ok: S4 unrelated -->\n```bash\nls\n```\n": [],
        '=== "Python"\n\n    ```python\n    x = 1\n    ```\n': [],
        '!!! example "Example"\n\n    ```python\n    x = 1\n    ```\n': ["K3"],
        "```python\n# simply a comment, currently\n```\n": ["K3"],
        # Waivers
        f"<!-- prose-ok: S4 a quoted error message -->\n{long}\n": [],
        f"{long} <!-- prose-ok: S4 a quoted error message -->\n": [],
        f"<!-- prose-ok: S4 -->\n{long}\n": ["S4"],
        f"<!-- prose-ok: S8 wrong rule -->\n{long}\n": ["S4"],
        "Simply. <!-- prose-ok: S8,S9 product name -->\n": [],
        "<!-- prose-ok: K3 a shell transcript -->\n```bash\nls\n```\n": [],
    }
    failed = 0
    for text, expected in cases.items():
        got = sorted(h.rule for h in lint_text("docs/x.md", text))
        if got != sorted(expected):
            print(f"self-test FAIL: {text!r}: got {got}, expected {sorted(expected)}")
            failed += 1
    return failed + _self_test_exemptions() + _self_test_baseline() + _self_test_hook()


def _self_test_hook() -> int:
    page = str(ROOT / "docs" / "x.md")
    cases = {
        json.dumps({"tool_input": {"file_path": page}}): "docs/x.md",
        json.dumps({"tool_input": {"file_path": str(ROOT / "README.md")}}): None,
        json.dumps({"tool_input": {"file_path": "/elsewhere/docs/x.md"}}): None,
        json.dumps({"tool_input": {}}): None,
        "not json": None,
    }
    failed = 0
    for event, expected in cases.items():
        if (got := hook_target(event)) != expected:
            print(f"self-test FAIL: hook {event!r}: got {got}, expected {expected}")
            failed += 1
    return failed


def _self_test_exemptions() -> int:
    failed = 0
    exempt: dict[tuple[str, str], list[str]] = {
        ("docs/reference/cli.md", "Items:\n\n" + "- i\n" * 8): [],
        ("docs/x.md", "---\ntype: reference\n---\nItems:\n\n" + "- i\n" * 8): [],
        ("docs/changelog.md", "It currently retries soon.\n"): [],
        ("docs/operate/upgrade-1.0.md", "Sandloom now supports it.\n"): [],
    }
    for (rel, text), expected in exempt.items():
        got = sorted(h.rule for h in lint_text(rel, text))
        if got != expected:
            print(f"self-test FAIL: {rel}: got {got}, expected {expected}")
            failed += 1
    scope = {
        ("docs/index.md", "# Sandloom\n"): True,
        ("docs/adr/0009-x.md", ""): False,
        ("docs/design/spec.md", ""): False,
        ("docs/archive/old.md", ""): False,
        ("docs/snippets/generated/README.md", ""): False,
        ("docs/reference/sdk/web.md", ""): False,
        ("docs/reference/testing.md", "# Testing\n"): True,
        (
            "docs/reference/x.md",
            "---\ntitle: x\n---\n\n<!-- Generated by y -->\n",
        ): False,
        ("docs/reference/x.md", "<!-- GENERATED from z -->\n# X\n"): False,
        ("README.md", ""): False,
        ("docs/x.txt", ""): False,
    }
    for (rel, text), expected_in in scope.items():
        if in_scope(rel, text) != expected_in:
            print(f"self-test FAIL: in_scope({rel!r}) != {expected_in}")
            failed += 1
    lines = lint_text("docs/x.md", "# Title\n\nOne.\n\nIt is simply done.\n")
    if [h.render() for h in lines] != ['docs/x.md:5: S8 "simply"']:
        print(f"self-test FAIL: render {lines}")
        failed += 1
    return failed


def _self_test_baseline() -> int:
    failed = 0
    base = {"a.md": {"S4": 2, "S8": 1}, "b.md": {"K3": 1}}
    cur = {"a.md": {"S4": 3}, "c.md": {"S9": 1}}
    over, dropped = compare(cur, base)
    if over != [("a.md", "S4", 3, 2), ("c.md", "S9", 1, 0)] or dropped != [
        ("a.md", "S8", 0, 1),
        ("b.md", "K3", 0, 1),
    ]:
        print(f"self-test FAIL: compare {over} {dropped}")
        failed += 1
    if lowered(cur, base) != {"a.md": {"S4": 2, "S8": 0}, "b.md": {"K3": 0}}:
        print(f"self-test FAIL: lowered {lowered(cur, base)}")
        failed += 1
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "b.json")
        write_baseline(path, lowered(cur, base))
        if load_baseline(path) != {"a.md": {"S4": 2}}:
            print(f"self-test FAIL: baseline round trip {load_baseline(path)}")
            failed += 1
    return failed


def self_test() -> int:
    failed = _self_test_rules()
    print("self-test:", "FAIL" if failed else "ok")
    return 1 if failed else 0


def _summary(hits: list[Hit]) -> None:
    for rule, n in sorted(Counter(h.rule for h in hits).items()):
        print(f"{n:6d}  {rule}")
    print()
    for path, n in Counter(h.path for h in hits).most_common(15):
        print(f"{n:6d}  {path}")


def _strict(args: list[str], summary: bool) -> int:
    hits = scan_paths(ROOT, args)
    for h in hits:
        print(h.render())
    if summary:
        _summary(hits)
    if hits:
        print(
            f"\n{len(hits)} prose-style hit(s) (docs-writing skill, Style and "
            "Code). Fix each, or waive the line with "
            "<!-- prose-ok: <rule> <reason> -->.",
            file=sys.stderr,
        )
        return 1
    return 0


def _ratchet(hits: list[Hit], baseline: dict[str, dict[str, int]]) -> int:
    over, dropped = compare(counts(hits), baseline)
    for path, rule, n, b in over:
        for h in hits:
            if h.path == path and h.rule == rule:
                print(h.render())
        print(f"{path}: {rule} {n} hit(s), baseline {b}\n")
    for path, rule, n, b in dropped:
        print(f"{path}: {rule} dropped to {n} from {b}", file=sys.stderr)
    if dropped:
        print(
            f"{len(dropped)} count(s) dropped: tighten the baseline with "
            "python3 tools/docs/docs_prose_lint.py --update-baseline",
            file=sys.stderr,
        )
    if over:
        print(
            f"{len(over)} (file, rule) count(s) above the baseline. Fix the new "
            "hits (`python3 tools/docs/docs_prose_lint.py PATH` lists every hit "
            "of a page), or waive a line with <!-- prose-ok: <rule> <reason> -->.",
            file=sys.stderr,
        )
        return 1
    return 0


def hook_target(event: str, root: Path = ROOT) -> str | None:
    """The page a PostToolUse event edited, when this lint covers it."""
    try:
        data = json.loads(event)
    except json.JSONDecodeError:
        return None
    tool_input = data.get("tool_input") if isinstance(data, dict) else None
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(path, str):
        return None
    try:
        rel = Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    return rel if in_scope(rel) else None


def _hook() -> int:
    rel = hook_target(sys.stdin.read())
    if rel is None or not (ROOT / rel).is_file():
        return 0
    text = _read(ROOT / rel) or ""
    report = [h.render() for h in lint_text(rel, text)]
    code = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, str(ROOT / "tools/docs/docs_code_check.py"), rel],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if code.returncode == 1:
        report += [ln for ln in code.stdout.splitlines() if ln.startswith(rel)]
    if not report:
        return 0
    print(
        "\n".join(report)
        + "\nFix the hits in the text you changed (tells.md, style.md), or waive the"
        " line with its reason; leave the others unless the task is about them.",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    if "--hook" in argv:
        return _hook()
    args = [a for a in argv if not a.startswith("--")]
    if args:
        return _strict(args, "--summary" in argv)
    hits = scan(ROOT, tracked_files(ROOT))
    if "--summary" in argv:
        _summary(hits)
        return 0
    if "--reset-baseline" in argv:
        write_baseline(BASELINE, counts(hits))
        print(f"wrote {BASELINE.relative_to(ROOT)} from {len(hits)} hit(s)")
        return 0
    baseline = load_baseline(BASELINE)
    if "--update-baseline" in argv:
        write_baseline(BASELINE, lowered(counts(hits), baseline))
        _, dropped = compare(counts(hits), baseline)
        print(f"lowered {len(dropped)} baseline count(s) in {BASELINE.name}")
        return 0
    return _ratchet(hits, baseline)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
