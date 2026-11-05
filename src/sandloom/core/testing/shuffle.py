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

A file holding a test marked ``slow`` comes first (the slow files in their
seeded order, then the others in theirs). Under pytest-xdist a run lasts at
least as long as its longest file, and a long file handed out last makes it
that much longer. With a seed the plugin also turns off xdist's
``--loadscope-reorder``, which would otherwise hand out the files of
``--dist loadfile`` by test count, largest first, so a slow one-test file
always started last and the seeded file order was lost.

``--sandloom-shard=K/N`` (with a seed) keeps the K-th of N parts of the
shuffled run: whole files, dealt in the seeded order to the part with the
fewest tests so far. The N parts of one seed are disjoint and together
are the whole selection; another seed deals the files differently, so two
files that never meet in one process under one seed meet under others.

Imports nothing from the framework: it only reorders the collected items.
"""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict
from typing import Any

import pytest

_SEED_KEY = pytest.StashKey[int]()
_SHARD_KEY = pytest.StashKey[tuple[int, int, int, int]]()
#: A file with a test marked this way is handed out first (see above).
SLOW_MARKER = "slow"


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
    parser.getgroup("sandloom").addoption(
        "--sandloom-shard",
        action="store",
        default=None,
        metavar="K/N",
        help=(
            "with --sandloom-shuffle: run only the K-th of N disjoint parts "
            "(whole files, balanced by test count) of the shuffled run"
        ),
    )


def _shard(config: pytest.Config) -> tuple[int, int] | None:
    raw = config.getoption("sandloom_shard", None)
    if raw is None:
        return None
    try:
        index, total = (int(part) for part in str(raw).split("/"))
    except ValueError:
        index = total = 0
    if not 1 <= index <= total:
        raise pytest.UsageError(
            f"--sandloom-shard={raw}: expected K/N with 1 <= K <= N"
        )
    if config.getoption("sandloom_shuffle", None) is None:
        raise pytest.UsageError("--sandloom-shard needs --sandloom-shuffle=<seed>")
    return index, total


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
    _shard(config)
    if _seed(config) is not None and hasattr(config.option, "loadscopereorder"):
        # The controller hands out the files in the seeded order (see above).
        config.option.loadscopereorder = False


class _XdistHooks:
    """Registered only when xdist is installed (its hook would be unknown)."""

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node: Any) -> None:
        seed = _seed(node.config)
        if seed is not None:
            node.workerinput["sandloom_shuffle_seed"] = seed


def pytest_report_header(config: pytest.Config) -> str | None:
    seed = _seed(config)
    if seed is None:
        return None
    shard = _shard(config)
    return f"sandloom-shuffle: seed {seed}" + (
        "" if shard is None else f", shard {shard[0]}/{shard[1]}"
    )


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
    slow = {
        name
        for name, tests in by_file.items()
        if any(t.get_closest_marker(SLOW_MARKER) is not None for t in tests)
    }
    files = sorted(by_file, key=lambda n: (n not in slow, _rank(seed, n)))
    shard = _shard(config)
    if shard is not None:
        index, total = shard
        kept = _deal(files, {name: len(by_file[name]) for name in files}, total)[
            index - 1
        ]
        dropped = [i for name in files if name not in kept for i in by_file[name]]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        config.stash[_SHARD_KEY] = (
            len(kept),
            len(files),
            len(items) - len(dropped),
            len(items),
        )
        files = [name for name in files if name in kept]
    shuffled: list[pytest.Item] = []
    for name in files:
        shuffled.extend(sorted(by_file[name], key=lambda i: _rank(seed, i.nodeid)))
    items[:] = shuffled


def _deal(files: list[str], sizes: dict[str, int], total: int) -> list[set[str]]:
    """Deal ``files`` (already in their seeded order) to ``total`` parts, each
    to the part with the fewest tests so far (the lowest index on a tie)."""
    parts: list[set[str]] = [set() for _ in range(total)]
    counts = [0] * total
    for name in files:
        smallest = counts.index(min(counts))
        parts[smallest].add(name)
        counts[smallest] += sizes[name]
    return parts


def _rank(seed: int, name: str) -> bytes:
    """A seeded sort key: the order of two names depends only on the seed and
    the names, so a replay with the same seed is the same order even on a
    host that collected the tests in a different order."""
    return hashlib.blake2b(f"{seed}:{name}".encode(), digest_size=8).digest()


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    seed = _seed(config)
    if seed is None:
        return
    shard = _shard(config)
    replay = f"--sandloom-shuffle={seed}"
    if shard is not None:
        replay += f" --sandloom-shard={shard[0]}/{shard[1]}"
        if _SHARD_KEY in config.stash:
            files, all_files, tests, all_tests = config.stash[_SHARD_KEY]
            terminalreporter.write_line(
                f"sandloom-shuffle: shard {shard[0]}/{shard[1]} ran {files} of "
                f"{all_files} files, {tests} of {all_tests} tests"
            )
    terminalreporter.write_line(f"sandloom-shuffle: seed {seed} (replay: {replay})")
