"""pytest plugin: run the collected tests in a seeded random order.

Every test must pass alone, in any order, serial or parallel. A parallel run
with a fixed distribution hides a test that only passes because an earlier
one left a registry, a cache or a row behind; a shuffled serial run finds it.

    pytest -p sandloom.core.testing.shuffle --sandloom-shuffle=random     # a fresh seed
    pytest -p sandloom.core.testing.shuffle --sandloom-shuffle=1234       # replay a
    seed

The order is shuffled in two levels: the test files, then the tests inside
each file (a file's module-scoped fixtures are still set up once). The seed
is printed in the report header and again in the terminal summary, so a
failure replays with ``--sandloom-shuffle=<seed>``. Without the option the plugin
changes nothing.

Imports nothing from the framework: it only reorders the collected items.
"""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict
from typing import Any

import pytest

_SEED_KEY = pytest.StashKey[int]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("sandloom").addoption(
        "--sandloom-shuffle",
        action="store",
        default=None,
        metavar="SEED",
        help=(
            "run the tests in a seeded random order (files, then the tests "
            "inside each file); SEED is an integer, or 'random' for a fresh one"
        ),
    )


def _seed(config: pytest.Config) -> int | None:
    if _SEED_KEY in config.stash:
        return config.stash[_SEED_KEY]
    raw = config.getoption("sandloom_shuffle", None)
    if raw is None:
        return None
    seed = random.SystemRandom().randrange(1, 10**8) if raw == "random" else int(raw)
    # xdist: every worker must collect the SAME order (the controller compares
    # the collections), so the controller's seed is handed to the workers.
    worker_input = getattr(config, "workerinput", None)
    if worker_input is not None and "sandloom_shuffle_seed" in worker_input:
        seed = int(worker_input["sandloom_shuffle_seed"])
    config.stash[_SEED_KEY] = seed
    return seed


def pytest_configure(config: pytest.Config) -> None:
    if config.pluginmanager.hasplugin("xdist"):
        config.pluginmanager.register(_XdistHooks(), "sandloom-shuffle-xdist")
    _seed(config)


class _XdistHooks:
    """Registered only when xdist is installed (its hook would be unknown)."""

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node: Any) -> None:
        seed = _seed(node.config)
        if seed is not None:
            node.workerinput["sandloom_shuffle_seed"] = seed


def pytest_report_header(config: pytest.Config) -> str | None:
    seed = _seed(config)
    return None if seed is None else f"sandloom-shuffle: seed {seed}"


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    seed = _seed(config)
    if seed is None:
        return
    by_file: dict[str, list[pytest.Item]] = defaultdict(list)
    for item in items:
        by_file[item.nodeid.split("::", 1)[0]].append(item)
    shuffled: list[pytest.Item] = []
    for name in sorted(by_file, key=lambda n: _rank(seed, n)):
        shuffled.extend(sorted(by_file[name], key=lambda i: _rank(seed, i.nodeid)))
    items[:] = shuffled


def _rank(seed: int, name: str) -> bytes:
    """A seeded sort key: the order of two names depends only on the seed and
    the names, so a replay with the same seed is the same order even on a
    host that collected the tests in a different order."""
    return hashlib.blake2b(f"{seed}:{name}".encode(), digest_size=8).digest()


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    seed = _seed(config)
    if seed is not None:
        terminalreporter.write_line(
            f"sandloom-shuffle: seed {seed} (replay: --sandloom-shuffle={seed})"
        )
