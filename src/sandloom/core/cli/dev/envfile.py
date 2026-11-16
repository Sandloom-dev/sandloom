"""The checkout's ``.env`` block and its ``.env.test.local``.

``sandloom dev up`` owns ONE marked block of ``.env``::

    # >>> sandloom dev (managed by `sandloom dev up`; edits inside are replaced)
    DATABASE_URL=...
    INFRA='{"cache":{"default":{"url":"redis://..."}}, ...}'
    # <<< sandloom dev

and nothing else: the rest of the file is the user's. A file without the
block gets it appended, and a ``DATABASE_URL`` line outside it is commented
out with a note (it would win otherwise: the last line of a dotenv file wins).

``INFRA`` is one variable, so the user's own ``INFRA`` and the managed one
cannot both stand: the block carries the MERGE. The managed instances (the
checkout's Redis urls) are the base and the user's ``INFRA`` is laid over them
per instance, option by option, so the user's keys win. The user's line is
the source and stays where it is; one above the block is left as written (the
block comes later and wins), one below it would win over the block, so it is
commented out with a note that keeps it readable as the source (a later
``dev up`` merges it again). An ``INFRA`` that is not a JSON object is refused
(:class:`InvalidInfra`), never skipped. The file is written atomically, keeps its
mode (0600 stays 0600) and, when it is a symlink, is written through the link.
A torn block (a begin marker without its end marker, say after a bad merge)
deletes nothing: every line stays, the managed variables in it are commented
out with a note, the marker is replaced by a note, and a fresh block is
appended. The write is a cheap compare-and-swap: the file is read again just
before it is replaced, and an edit that landed meanwhile is merged once more.
A worktree without ``.env`` gets one from ``.env.example`` with a fresh
``JWT_SECRET`` first.

``.env.test.local`` (gitignored) holds the ``SANDLOOM_TEST_*`` URLs of the
repository's test servers, read by the test harness after ``.env.test``.
Every URL names ``127.0.0.1``, never ``localhost`` (which may resolve to
``::1`` and reach a different server with the same credentials).
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Mapping
from pathlib import Path

from sandloom.core.cli.dev.state import atomic_write

BEGIN = "# >>> sandloom dev (managed by `sandloom dev up`; edits inside are replaced)"
END = "# <<< sandloom dev"
_BEGIN_PREFIX = "# >>> sandloom dev"
_END_PREFIX = "# <<< sandloom dev"
INFRA = "INFRA"
MANAGED = ("DATABASE_URL", INFRA)
_NOTE = "# (moved into the sandloom dev block below) "
_INFRA_NOTE = "# (your INFRA, merged into the sandloom dev block above) "
_TORN = (
    "# (sandloom dev: a block began here without its end marker; its lines "
    "were kept and a new block is below)"
)
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
        "# DATABASE_URL and INFRA (the Redis url of each instance).\n"
        f"{END}\n"
    )


class InvalidInfra(ValueError):
    """An ``INFRA`` that cannot be merged or written into the block."""


def _line(key: str, value: str) -> str:
    if key != INFRA:
        return f"{key}={value}"
    if "'" in value or "\\" in value:
        raise InvalidInfra(
            "INFRA holds a quote or a backslash, which a single-quoted .env value "
            "cannot carry as JSON: write the urls percent-encoded (a password with "
            "a quote or backslash included)"
        )
    return f"{key}='{value}'"


def render_block(values: dict[str, str]) -> str:
    lines = [BEGIN, *(_line(key, values[key]) for key in MANAGED if key in values)]
    return "\n".join([*lines, END]) + "\n"


#: ``{capability: {instance: {option: value}}}``, the shape of ``INFRA``.
InfraMap = dict[str, dict[str, dict[str, object]]]


#: The option carrying the Redis url of an instance, by the driver the app's
#: composition selects for it. An instance on another driver is not Redis and
#: gets nothing here.
_URL_OPTION = {"redis": "url", "streams": "url", "celery": "broker"}
#: Capabilities whose instances may lose their keys (a cache, pub/sub): they
#: take the cache index. Every other Redis instance holds state that must
#: survive (revocation marks, the lease, the event streams, the broker's
#: queues) and takes the persistent index, never the cache's.
_CACHE_CAPABILITIES = frozenset({"cache", "broadcast"})


def managed_infra(
    selection: Mapping[str, Mapping[str, Mapping[str, object]]],
    *,
    cache_url: str,
    persistent_url: str,
    memory_bus: bool,
) -> InfraMap:
    """The Redis url of each instance of ``selection`` (the composition's
    ``DEFAULT_INFRA``) that runs on Redis, as the managed ``INFRA`` of a
    checkout. An entry carries no ``driver``: the composition's is kept. An
    instance that names its url already keeps it; ``memory_bus`` (the in-process
    bus is selected by ``EVENT_BUS_BACKEND``) leaves the event bus out."""
    managed: InfraMap = {}
    for capability, instances in selection.items():
        for name, spec in instances.items():
            option = _URL_OPTION.get(str(spec.get("driver")))
            if option is None or option in spec:
                continue
            if capability == "eventbus" and memory_bus:
                continue
            url = cache_url if capability in _CACHE_CAPABILITIES else persistent_url
            managed.setdefault(capability, {})[name] = {option: url}
    return managed


def _infra_object(raw: str, source: str) -> InfraMap:
    """``raw`` as ``{capability: {instance: {option: value}}}``, else
    :class:`InvalidInfra` naming ``source``."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidInfra(f"{source} is not valid JSON ({exc})") from exc
    if not isinstance(data, dict) or not all(
        isinstance(instances, dict)
        and all(isinstance(spec, dict) for spec in instances.values())
        for instances in data.values()
    ):
        raise InvalidInfra(
            f'{source} must be {{"<capability>": {{"<instance>": {{...options}}}}}}'
        )
    return data


def _user_infra_raw(text: str) -> tuple[str | None, bool]:
    """``(the raw value, whether it was already set aside)``: the winning
    ``INFRA`` assignment outside the block, else the last one a previous run
    commented out (:data:`_INFRA_NOTE`). ``(None, False)`` without either."""
    before, _, after = _split(text)
    active: str | None = None
    noted: str | None = None
    for line in [*before, *after]:
        match = _ASSIGN.match(line)
        if match and match.group(1) == INFRA:
            active = match.group(2)
        elif line.startswith(_INFRA_NOTE):
            kept = _ASSIGN.match(line.removeprefix(_INFRA_NOTE))
            if kept and kept.group(1) == INFRA:
                noted = kept.group(2)
    if active is not None:
        return active, False
    return noted, noted is not None


def user_infra(text: str) -> InfraMap:
    """The user's own ``INFRA`` (``{}`` without one); :class:`InvalidInfra`
    when it is not an ``INFRA`` object."""
    raw, _ = _user_infra_raw(text)
    if raw is None:
        return {}
    return _infra_object(raw.strip().strip("'\""), "the INFRA in .env")


def merge_infra(managed: InfraMap, user: InfraMap) -> InfraMap:
    """``managed`` with ``user`` laid over it, instance by instance and option
    by option: the user's keys win, an instance only one side names is kept."""
    merged = {
        cap: {name: dict(spec) for name, spec in inst.items()}
        for cap, inst in managed.items()
    }
    for capability, instances in user.items():
        bucket = merged.setdefault(capability, {})
        for name, spec in instances.items():
            bucket[name] = {**bucket.get(name, {}), **spec}
    return merged


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
    if end is None:  # a torn block: keep every line, no block
        return [*lines[:begin], _TORN, *lines[begin + 1 :]], None, []
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


def winning_assignment(text: str, key: str) -> tuple[str, bool]:
    """``(value, outside)`` of the assignment of ``key`` that wins, as in a
    dotenv file: the last one in the file, block included. ``outside``: that
    line is not in the managed block. ``("", False)`` without one."""
    before, block, after = _split(text)
    found: tuple[str, bool] = ("", False)
    for lines, outside in ((before, True), (block or [], False), (after, True)):
        for line in lines:
            match = _ASSIGN.match(line)
            if match and match.group(1) == key:
                found = (match.group(2).strip().strip("'\""), outside)
    return found


def replace_block(text: str, values: dict[str, str]) -> str:
    """``text`` with the block holding ``values``; every other line kept.

    ``values["INFRA"]`` (the managed instances, JSON) is merged with the
    user's own ``INFRA`` (:func:`user_infra`, theirs win) before it is
    written; a user's ``INFRA`` set aside by an earlier run keeps being
    merged even when ``values`` carries none."""
    before, block, after = _split(text)
    values = dict(values)
    if INFRA in values or _user_infra_raw(text)[1]:
        managed = _infra_object(values.get(INFRA, "{}"), "the managed INFRA")
        values[INFRA] = json.dumps(
            merge_infra(managed, user_infra(text)), separators=(",", ":")
        )
    merging = INFRA in values

    def comment_out(lines: list[str], *, below: bool) -> list[str]:
        out = []
        for line in lines:
            match = _ASSIGN.match(line)
            if match and match.group(1) == "DATABASE_URL":
                out.append(_NOTE + line)
            elif match and match.group(1) == INFRA and merging and below:
                # Below the block it would win over the merge: set aside, kept
                # readable as the source of the next merge.
                out.append(_INFRA_NOTE + line)
            else:
                out.append(line)
        return out

    rendered = render_block(values).rstrip("\n").splitlines()
    if block is None:
        head = comment_out(before, below=False)
        while head and not head[-1].strip():
            head.pop()
        lines = [*head, "", *rendered] if head else rendered
    else:
        lines = [
            *comment_out(before, below=False),
            *rendered,
            *comment_out(after, below=True),
        ]
    return "\n".join(lines) + "\n"


class EnvFileChanged(RuntimeError):
    """The ``.env`` kept changing while ``sandloom dev up`` rewrote it."""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_env_block(path: Path, values: dict[str, str]) -> bool:
    """Rewrite ``path``'s block (``True``: the file changed).

    Compare-and-swap: the file is read again just before it is replaced; an
    edit that landed in between is merged once more (one retry)."""
    text = _read(path)
    for _ in range(2):
        new = replace_block(text, values)
        if new == text:
            return False
        current = _read(path)
        if current == text:
            atomic_write(path, new)
            return True
        text = current
    raise EnvFileChanged(
        f"{path} kept changing while `sandloom dev up` wrote its block; run it again"
    )


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
    "EnvFileChanged",
    "EXAMPLE_FILE",
    "INFRA",
    "InfraMap",
    "InvalidInfra",
    "MANAGED",
    "TEST_LOCAL_FILE",
    "empty_block",
    "env_mode",
    "exported_drift",
    "managed_infra",
    "merge_infra",
    "read_block",
    "render_block",
    "replace_block",
    "seed_env",
    "user_infra",
    "write_env_block",
    "write_test_local",
]
