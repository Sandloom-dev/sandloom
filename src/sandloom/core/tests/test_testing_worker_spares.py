"""The URL movers harness families install (``sandloom.driverkit.testing_isolation``,
one per family name) are the only thing that decides what a worker's URL becomes:
the kernel's hook helper applies every one to the options a driver names, for a worker id ``gw<N>``, and to
nothing else. The Redis layout (stride, the spares of replacement workers) is the
``sandloom-redis`` family's and is tested in
``packages/sandloom-redis/tests/test_harness_isolation.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from sandloom.driverkit.testing_isolation import isolated_url_options, set_url_mover


_FAMILIES = ("alpha", "beta", "other")


@pytest.fixture(autouse=True)
def _no_mover() -> Iterator[None]:
    for family in _FAMILIES:
        set_url_mover(family, None)
    yield
    for family in _FAMILIES:
        set_url_mover(family, None)


def _suffix(url: str, number: int) -> str:
    return f"{url}#gw{number}"


def _owning(scheme: str) -> Callable[[str, int], str]:
    """A mover of a fake family that owns ``scheme`` URLs and leaves others."""

    def move(url: str, number: int) -> str:
        return f"{url}#{scheme}{number}" if url.startswith(f"{scheme}://") else url

    return move


def test_two_families_each_move_their_own_urls_of_one_options_set() -> None:
    set_url_mover("alpha", _owning("alpha"))
    set_url_mover("beta", _owning("beta"))
    options = {"broker": "alpha://h/1", "backend": "beta://h/2", "other": "gamma://h/3"}
    moved = isolated_url_options(options, "gw4", keys=("broker", "backend", "other"))
    assert moved == {
        "broker": "alpha://h/1#alpha4",
        "backend": "beta://h/2#beta4",
        "other": "gamma://h/3",
    }


def test_installing_a_mover_for_a_family_replaces_only_that_familys_mover() -> None:
    set_url_mover("alpha", _owning("alpha"))
    set_url_mover("beta", _owning("beta"))
    set_url_mover("alpha", lambda url, number: url.replace("alpha://", "moved://"))
    moved = isolated_url_options({"a": "alpha://h", "b": "beta://h"}, "gw1", keys=("a", "b"))
    assert moved == {"a": "moved://h", "b": "beta://h#beta1"}
    set_url_mover("alpha", None)
    kept = isolated_url_options({"a": "alpha://h", "b": "beta://h"}, "gw1", keys=("a", "b"))
    assert kept == {"a": "alpha://h", "b": "beta://h#beta1"}


def test_a_mover_moves_each_named_url_for_the_numbered_worker() -> None:
    set_url_mover("alpha", _suffix)
    options = {"broker": "a://x/1", "backend": "a://x/2", "other": "a://x/3", "n": 1}
    moved = isolated_url_options(options, "gw7", keys=("broker", "backend"))
    assert moved == {
        "broker": "a://x/1#gw7",
        "backend": "a://x/2#gw7",
        "other": "a://x/3",  # not named: untouched
        "n": 1,
    }
    assert options["broker"] == "a://x/1", "the options given are never mutated"


@pytest.mark.parametrize("worker_id", ["master", "gw", "gwx", "worker1", ""])
def test_a_worker_id_that_is_not_gw_n_leaves_the_options(worker_id: str) -> None:
    set_url_mover("alpha", _suffix)
    options = {"url": "a://x/1"}
    assert isolated_url_options(options, worker_id) == options


def test_without_a_mover_the_options_come_back_as_given() -> None:
    """No family claimed a URL: the kernel moves nothing on its own."""
    options = {"url": "redis://h:6379/1"}
    assert isolated_url_options(options, "gw1") == options


def test_a_non_string_option_is_not_handed_to_the_mover() -> None:
    seen: list[str] = []
    set_url_mover("alpha", lambda url, number: seen.append(url) or url)
    options = {"url": None}
    assert isolated_url_options(options, "gw1") == options
    assert seen == []
