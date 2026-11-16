"""Ratchet: `sandloom dev` writes a url for every driver that reads one.

The managed ``INFRA`` of a checkout names, per driver, the option holding the
Redis url (``envfile._URL_OPTION``). A driver the table misses would get no url
in the block and fail startup only at its first use, so every installed driver
that takes a url from a flat setting (``settings_fill.urls``) must be in it, and
every entry must still name an installed driver.

The table is a stopgap: the settings that feed ``settings_fill`` are removed by
the flat-urls Z slice, and the url option then moves onto the provider class
(a ClassVar read generically); this test goes with the table.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import entry_points

import pytest

from sandloom.core.cli.dev.envfile import _URL_OPTION
from sandloom.core.infra.ports._base import InfraProvider


def _installed() -> dict[str, type[InfraProvider]]:
    """``{"<capability>.<driver>": provider class}`` of every installed driver."""
    found: dict[str, type[InfraProvider]] = {}
    for entry in entry_points(group="sandloom.drivers"):
        loaded = entry.load()
        if isinstance(loaded, type) and issubclass(loaded, InfraProvider):
            found[entry.name] = loaded
    return found


def _gaps(
    installed: Mapping[str, type[InfraProvider]], table: Mapping[str, str]
) -> list[str]:
    drivers = {key.partition(".")[2] for key in installed}
    gaps = [
        f"{key}: takes {sorted(cls.settings_fill.urls)} from a flat setting but "
        f"`{key.partition('.')[2]}` has no entry (or another option) in _URL_OPTION"
        for key, cls in sorted(installed.items())
        if cls.settings_fill is not None
        and cls.settings_fill.urls
        and table.get(key.partition(".")[2]) not in cls.settings_fill.urls
    ]
    gaps += [
        f"_URL_OPTION names `{driver}`, which no installed driver publishes"
        for driver in sorted(set(table) - drivers)
    ]
    return gaps


def test_every_installed_url_driver_is_in_the_dev_url_table() -> None:
    installed = _installed()
    # The table is checked against the drivers of this repository: with none
    # installed the test would pass over nothing.
    assert {"cache.redis", "eventbus.streams", "taskqueue.celery"} <= set(installed)
    assert _gaps(installed, _URL_OPTION) == []


def test_the_ratchet_reports_a_missing_and_a_stale_entry() -> None:
    installed = _installed()
    without = {d: o for d, o in _URL_OPTION.items() if d != "celery"}
    assert any("taskqueue.celery" in gap for gap in _gaps(installed, without))
    wrong = {**_URL_OPTION, "celery": "not-an-option"}
    assert any("taskqueue.celery" in gap for gap in _gaps(installed, wrong))
    stale = {**_URL_OPTION, "ghost": "url"}
    assert any("`ghost`" in gap for gap in _gaps(installed, stale))
    with pytest.raises(AssertionError):
        assert _gaps(installed, without) == []
