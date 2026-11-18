"""The files of a new app that hold the stack's backing server, and their completion.

``sandloom new`` renders every file of the app, but the lines that name the stack's
backing server (its compose blocks, its URLs, the label in the ``.env`` block)
come from the package of the stack's driver (:class:`StackService`), which a bare
``pip install sandloom`` lacks. Those lines are written as ``@@STACK:<key>@@``
tokens first; the values replace them at once when the package is importable in the
running environment, otherwise ``sandloom add stack`` does it in the new app's own
environment, after ``uv sync`` installed the package. Both paths run :func:`fill`
over the same text, so the files are the same bytes.

The kernel holds no value: :func:`values` asks the stack for each.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import sandloom.core.cli.dev.envfile as _envfile
import sandloom.core.cli.stack_service as _stack

#: What a missing compose port says (``sandloom dev up`` sets it).
PORT_HINT = "start the services with: uv run sandloom dev up"
#: The values a stack supplies, by the key of their ``@@STACK:<key>@@`` token.
KEYS = (
    "dev_block",
    "infra_test",
    "infra_compose",
    "infra_sqlite",
    "kv_dev",
    "kv_dev_test",
    "kv_prod",
    "kv_ci",
    "kv_name",
    "kv_volume",
)
#: The files of ``sandloom new`` that carry a token (a test renders the app and
#: finds the same set).
FILES = (
    ".env",
    ".env.example",
    ".env.test",
    ".github/workflows/ci.yml",
    "README.md",
    "deploy/docker-compose.dev.yml",
    "deploy/docker-compose.yml",
)
_TOKEN = re.compile(r"@@STACK:([a-z_]+)@@")


def token(key: str) -> str:
    """The placeholder of value ``key`` in a rendered file."""
    if key not in KEYS:
        raise KeyError(key)
    return f"@@STACK:{key}@@"


def infra_json(cache: str, persistent: str, *, eventbus: bool = True) -> str:
    """The ``INFRA`` of the keyed instances ``DEFAULT_INFRA`` (``_APP_INIT``)
    selects, one explicit url each (an entry carries no ``driver``: the
    composition's is kept; the urls are the stack service's: ``server.url``).
    The instances that may lose their keys (cache, broadcast) take ``cache``;
    every one that holds state (the security store, the beat lease, the event
    streams, the task-queue broker) takes ``persistent``, never the cache's: a
    persistent instance sharing the cache's host, port and database logs a
    startup warning. ``eventbus=False`` leaves the event bus out (the
    in-process bus is selected)."""
    infra: dict[str, dict[str, dict[str, str]]] = {
        "cache": {"default": {"url": cache}},
        "broadcast": {"default": {"url": cache}},
        "atomic_store": {"security": {"url": persistent}},
        "lease": {"beat": {"url": persistent}},
        "taskqueue": {"default": {"broker": persistent}},
    }
    if eventbus:
        infra["eventbus"] = {"default": {"url": persistent}}
    return json.dumps(infra, separators=(",", ":"))


def values(stack: _stack.StackService) -> dict[str, str]:
    """The text of each :data:`KEYS` entry, as ``stack`` describes its server."""
    ci_server = stack.server("127.0.0.1", stack.ci_port)
    compose_server = stack.server(stack.name, stack.container_port)
    local_server = stack.server("127.0.0.1", stack.local_port)
    return {
        "dev_block": _envfile.empty_block(stack.label),
        # CI's test server (Postgres :5433, the keyed server on its own port):
        # the cache on one database, the persistent instances on another. The
        # in-process bus is selected there.
        "infra_test": infra_json(
            ci_server.url(stack.test_cache_index),
            ci_server.url(stack.test_broker_index),
            eventbus=False,
        ),
        "infra_compose": infra_json(compose_server.url(1), compose_server.url(2)),
        "infra_sqlite": infra_json(
            local_server.url(0), local_server.url(1), eventbus=False
        ),
        "kv_dev": stack.dev_compose(PORT_HINT, test=False),
        "kv_dev_test": stack.dev_compose(PORT_HINT, test=True),
        "kv_prod": stack.prod_compose,
        "kv_ci": stack.ci_service,
        "kv_name": stack.name,
        "kv_volume": stack.prod_volume,
    }


def fill(text: str, filled: dict[str, str]) -> str:
    """``text`` with every token replaced by its value in ``filled``."""

    def one(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in filled:
            raise _stack.StackServiceError(f"unknown stack token {match.group(0)}")
        return filled[key]

    return _TOKEN.sub(one, text)


def pending(root: Path) -> list[str]:
    """The files of the app at ``root`` that still carry a token."""
    return [
        rel
        for rel in FILES
        if (root / rel).is_file()
        and _TOKEN.search((root / rel).read_text(encoding="utf-8"))
    ]


def refusal(root: Path) -> str | None:
    """Why a command that reads the app's files cannot run yet, or ``None``: some
    still carry a token (``sandloom new`` ran without the stack's package and
    ``sandloom add stack`` has not run)."""
    left = pending(root)
    if not left:
        return None
    return (
        f"the stack's files are not written yet ({', '.join(left)}): run "
        "`uv run sandloom add stack` first"
    )


def recording(name: str) -> str:
    """The lines of ``pyproject.toml`` that record the app's stack, for
    ``sandloom add stack``. The default stack is not recorded: the files of a
    standard app stay what they were."""
    if name == _stack.DEFAULT_STACK:
        return ""
    return f'\n[tool.sandloom]\nstack = "{name}"\n'


def recorded(root: Path) -> str:
    """The name of the stack the app at ``root`` was made with (the default
    where ``pyproject.toml`` records none)."""
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise _stack.StackServiceError(f"{root / 'pyproject.toml'}: {exc}") from exc
    table = data.get("tool", {}).get("sandloom", {})
    name = table.get("stack", _stack.DEFAULT_STACK)
    return _stack.stack_choice(str(name)).name


def complete(root: Path, stack: _stack.StackService) -> list[Path]:
    """Write the stack's lines into the files of the app at ``root`` that still
    carry a token; returns the files changed (none when it is complete)."""
    filled = values(stack)
    changed: list[Path] = []
    for rel in pending(root):
        path = root / rel
        path.write_text(
            fill(path.read_text(encoding="utf-8"), filled), encoding="utf-8"
        )
        changed.append(path)
    return changed
