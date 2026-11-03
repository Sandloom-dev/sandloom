"""The checkout's ``.env`` block and its ``.env.test.local``.

``sandloom dev up`` owns ONE marked block of ``.env``::

    # >>> sandloom dev (managed by `sandloom dev up`; edits inside are replaced)
    DATABASE_URL=...
    CACHE_URL=...
    BROKER_URL=...
    # <<< sandloom dev

and nothing else: the rest of the file is the user's. A file without the
block gets it appended, and a ``DATABASE_URL`` / ``CACHE_URL`` / ``BROKER_URL``
line outside it is commented out with a note (it would win otherwise: the
last line of a dotenv file wins). The file is written atomically, keeps its
mode (0600 stays 0600) and, when it is a symlink, is written through the link.
A worktree without ``.env`` gets one from ``.env.example`` with a fresh
``JWT_SECRET`` first.

``.env.test.local`` (gitignored) holds the ``SANDLOOM_TEST_*`` URLs of the
repository's test servers, read by the test harness after ``.env.test``.
Every URL names ``127.0.0.1``, never ``localhost`` (which may resolve to
``::1`` and reach a different server with the same credentials).
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from sandloom.core.cli.dev.state import atomic_write

BEGIN = "# >>> sandloom dev (managed by `sandloom dev up`; edits inside are replaced)"
END = "# <<< sandloom dev"
_BEGIN_PREFIX = "# >>> sandloom dev"
_END_PREFIX = "# <<< sandloom dev"
MANAGED = ("DATABASE_URL", "CACHE_URL", "BROKER_URL")
_NOTE = "# (moved into the sandloom dev block below) "
ENV_FILE = ".env"
EXAMPLE_FILE = ".env.example"
TEST_LOCAL_FILE = ".env.test.local"
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_JWT = re.compile(r"^(\s*(?:export\s+)?JWT_SECRET\s*=).*$", re.MULTILINE)


def empty_block() -> str:
    """The block ``sandloom new`` writes: filled by the first ``dev up``."""
    return (
        f"{BEGIN}\n"
        "# Empty until `uv run sandloom dev up` fills it with this checkout's\n"
        "# DATABASE_URL, CACHE_URL and BROKER_URL.\n"
        f"{END}\n"
    )


def render_block(values: dict[str, str]) -> str:
    lines = [BEGIN, *(f"{key}={values[key]}" for key in MANAGED if key in values)]
    return "\n".join([*lines, END]) + "\n"


def _split(text: str) -> tuple[list[str], list[str] | None, list[str]]:
    """``(before, block or None, after)`` as lines (no newlines)."""
    lines = text.splitlines()
    begin = next(
        (i for i, ln in enumerate(lines) if ln.startswith(_BEGIN_PREFIX)), None
    )
    if begin is None:
        return lines, None, []
    end = next(
        (i for i in range(begin + 1, len(lines)) if lines[i].startswith(_END_PREFIX)),
        None,
    )
    if end is None:  # a torn block: everything after the marker was ours
        return lines[:begin], lines[begin + 1 :], []
    return lines[:begin], lines[begin + 1 : end], lines[end + 1 :]


def read_block(text: str) -> dict[str, str]:
    """The values in the managed block (``{}``: no block or an empty one)."""
    _, block, _ = _split(text)
    out: dict[str, str] = {}
    for line in block or []:
        match = _ASSIGN.match(line)
        if match:
            out[match.group(1)] = match.group(2).strip().strip("'\"")
    return out


def replace_block(text: str, values: dict[str, str]) -> str:
    """``text`` with the block holding ``values``; every other line kept."""
    before, block, after = _split(text)

    def comment_out(lines: list[str]) -> list[str]:
        out = []
        for line in lines:
            match = _ASSIGN.match(line)
            if match and match.group(1) in MANAGED:
                out.append(_NOTE + line)
            else:
                out.append(line)
        return out

    rendered = render_block(values).rstrip("\n").splitlines()
    if block is None:
        head = comment_out(before)
        while head and not head[-1].strip():
            head.pop()
        lines = [*head, "", *rendered] if head else rendered
    else:
        lines = [*comment_out(before), *rendered, *comment_out(after)]
    return "\n".join(lines) + "\n"


def write_env_block(path: Path, values: dict[str, str]) -> bool:
    """Rewrite ``path``'s block (``True``: the file changed)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    new = replace_block(text, values)
    if new == text:
        return False
    atomic_write(path, new)
    return True


def seed_env(root: Path) -> bool:
    """Create ``<root>/.env`` from ``.env.example`` with a fresh ``JWT_SECRET``
    when it is missing (a new worktree). ``True``: it was created."""
    env = root / ENV_FILE
    if env.exists() or env.is_symlink():
        return False
    example = root / EXAMPLE_FILE
    try:
        text = example.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    secret = secrets.token_hex(32)
    if _JWT.search(text):
        text = _JWT.sub(lambda m: m.group(1) + secret, text)
    else:
        text = text + ("" if not text or text.endswith("\n") else "\n")
        text += f"JWT_SECRET={secret}\n"
    atomic_write(env, text, mode=0o600)
    return True


def render_test_local(values: dict[str, str]) -> str:
    lines = [
        "# Written by `uv run sandloom dev up`: this checkout's test servers.",
        "# Not committed. The test harness reads it after .env.test; an exported",
        "# SANDLOOM_TEST_* wins. Each test run gets its own database and Redis",
        "# indexes on these servers.",
        *(f"{key}={value}" for key, value in values.items()),
    ]
    return "\n".join(lines) + "\n"


def write_test_local(root: Path, values: dict[str, str]) -> bool:
    path = root / TEST_LOCAL_FILE
    text = render_test_local(values)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    atomic_write(path, text)
    return True


def exported_drift(block: dict[str, str], environ: dict[str, str]) -> list[str]:
    """Variables exported in ``environ`` that differ from ``block`` (the
    process environment beats ``.env``)."""
    return [
        key
        for key in MANAGED
        if block.get(key) and environ.get(key) and environ[key] != block[key]
    ]


def env_mode(path: Path) -> int | None:
    try:
        return os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        return None


__all__ = [
    "BEGIN",
    "END",
    "ENV_FILE",
    "EXAMPLE_FILE",
    "MANAGED",
    "TEST_LOCAL_FILE",
    "empty_block",
    "env_mode",
    "exported_drift",
    "read_block",
    "render_block",
    "replace_block",
    "seed_env",
    "write_env_block",
    "write_test_local",
]
