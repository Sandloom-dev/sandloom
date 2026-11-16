"""Which ``SANDLOOM_TEST_*`` Redis overrides ``dev up`` writes.

The harness merges ``SANDLOOM_TEST_CACHE_URL`` / ``SANDLOOM_TEST_BROKER_URL``
into the ``INFRA`` of ``.env.test`` and raises ``UnmatchedOverrideError`` where
that ``INFRA`` declares no url on the ``default`` instance they replace. So
``dev up`` writes a name only where the composition selects the instance AND
``.env.test`` declares its url; the capability-to-name map is the harness's own.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from sandloom.core.cli.dev import commands, identity
from sandloom.core.cli.dev.state import State
from sandloom.core.testing import workers

_SELECTION = commands.CompositionSelection(
    {
        "cache": {"default": {"driver": "redis"}},
        "taskqueue": {"default": {"driver": "celery"}},
    },
    {"cache.redis": "url", "taskqueue.celery": "broker"},
)
_BOTH = {"CACHE_URL", "BROKER_URL"}
_CACHE = {"cache": {"default": {"url": "redis://127.0.0.1:6380/15"}}}
_QUEUE = {"taskqueue": {"default": {"broker": "redis://127.0.0.1:6380/14"}}}


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SANDLOOM_HOME", str(tmp_path / "home"))
    root = tmp_path / "shop"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "shop"\n')
    return root


def _written(
    checkout: Path,
    env_test: str | None,
    selection: commands.CompositionSelection,
) -> set[str]:
    environ = {k: v for k, v in os.environ.items() if k not in {"CI", "DOCKER_HOST"}}
    ctx = commands.Context(
        identity=identity.resolve(checkout, environ),
        environ=environ,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    state = State(
        repository=str(ctx.identity.repository),
        project=ctx.identity.project,
        machine=ctx.identity.machine,
        ports={"db": 1, "redis": 2, "postgres_test": 3, "redis_test": 4},
    )
    if env_test is not None:
        (checkout / ".env.test").write_text(env_test)
    urls = commands.test_urls(ctx, state, commands.checkout_row(ctx, state), selection)
    return {name.removeprefix(workers.OVERRIDE_PREFIX) for name in urls} - {
        "DATABASE_URL"
    }


def _env(infra: dict[str, Any]) -> str:
    return f"INFRA='{json.dumps(infra)}'\n"


@pytest.mark.parametrize(
    ("env_test", "selection", "names"),
    [
        (_env({**_CACHE, **_QUEUE}), _SELECTION, {"CACHE_URL", "BROKER_URL"}),
        # the selection has a task queue, ``.env.test`` declares none: the
        # harness would raise on SANDLOOM_TEST_BROKER_URL
        (_env(_CACHE), _SELECTION, {"CACHE_URL"}),
        # a declared url on another instance than ``default`` is none to replace
        (
            _env({"cache": {"aux": _CACHE["cache"]["default"]}, **_QUEUE}),
            _SELECTION,
            {"BROKER_URL"},
        ),
        # an empty selection (no composition) is judged by ``.env.test`` alone
        (_env(_CACHE), commands.CompositionSelection({}, {}), {"CACHE_URL"}),
        (_env({}), commands.CompositionSelection({}, {}), set()),
        # ``.env.test`` declares both, the composition has no task queue
        (
            _env({**_CACHE, **_QUEUE}),
            commands.CompositionSelection(
                {"cache": _SELECTION.infra["cache"]}, {"cache.redis": "url"}
            ),
            {"CACHE_URL"},
        ),
        # no INFRA in ``.env.test``, or no file: the harness has no instance for an
        # override and raises ``UnmatchedOverrideError``, so none is written
        ("JWT_SECRET=x\n", _SELECTION, set()),
        (None, _SELECTION, set()),
        # the harness reads ``.env.test`` with python-dotenv: the same three
        # inputs a hand parse gets wrong must still name both overrides
        (
            _env({**_CACHE, **_QUEUE}).rstrip() + "  # the shared test redis\n",
            _SELECTION,
            _BOTH,
        ),
        (
            "REDIS=redis://127.0.0.1:6380\n"
            'INFRA=\'{"cache":{"default":{"url":"${REDIS}/15"}},'
            '"taskqueue":{"default":{"broker":"${REDIS}/14"}}}\'\n',
            _SELECTION,
            _BOTH,
        ),
        (
            "INFRA='" + json.dumps({**_CACHE, **_QUEUE}, indent=2) + "'\n",
            _SELECTION,
            _BOTH,
        ),
        # an unresolved ``${VAR}`` is no Redis url
        (
            'INFRA=\'{"cache":{"default":{"url":"${NOT_SET_ANYWHERE}"}}}\'\n',
            _SELECTION,
            set(),
        ),
    ],
    ids=[
        "both",
        "queue-undeclared",
        "not-default",
        "empty-selection-cache",
        "empty-selection-nothing",
        "queue-unselected",
        "no-infra-in-env-test",
        "no-env-test",
        "trailing-comment",
        "interpolated-var",
        "multi-line-value",
        "unresolved-var",
    ],
)
def test_dev_up_writes_the_overrides_the_harness_can_apply(
    checkout: Path,
    env_test: str | None,
    selection: commands.CompositionSelection,
    names: set[str],
) -> None:
    assert _written(checkout, env_test, selection) == names


def test_the_names_are_the_harness_own_map() -> None:
    assert set(workers.INFRA_URL_CAPABILITIES) == set(workers.REDIS_VARS)


@pytest.mark.parametrize(
    ("infra", "names"),
    [
        (json.dumps({**_CACHE, **_QUEUE}), ("CACHE_URL", "BROKER_URL")),
        (json.dumps(_QUEUE), ("BROKER_URL",)),
        (json.dumps({"cache": {"default": {"url": "memory://"}}}), ()),
        ("{not json", ()),
        ("[]", ()),
        (None, ()),
    ],
)
def test_overrides_infra_can_take(infra: str | None, names: tuple[str, ...]) -> None:
    assert workers.overrides_infra_can_take(infra) == names
    for name in names:  # what it names, the harness applies without raising
        workers.infra_with_overrides(infra, {name: "redis://127.0.0.1:1/1"})
