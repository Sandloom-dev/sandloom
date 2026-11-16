"""Ratchet: `sandloom dev` writes a url for every driver that reads one.

The managed ``INFRA`` of a checkout names, per driver, the option holding the
Redis url (``envfile._URL_OPTION``). A driver the table misses would get no url
in the block and fail startup only at its first use, so every driver of the
repository's two driver packages that has a ``url`` or ``broker`` option must be
in it, under that option, and every entry must still name an installed driver.

The table is a stopgap: the url option moves onto the provider class (a ClassVar
read generically, so the kernel names no driver) once the Redis atomic store and
lease providers are free of their open task; this test goes with the table.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import entry_points

import pytest

from sandloom.core.cli.dev.envfile import _URL_OPTION
from sandloom.core.infra.ports._base import InfraProvider

#: The driver packages of this repository: a third party's driver is not held
#: to the table.
_REPOSITORY_PACKAGES = frozenset({"sandloom_redis", "sandloom_celery"})
_URL_OPTIONS = ("url", "broker")


def _installed() -> dict[str, type[InfraProvider]]:
    """``{"<capability>.<driver>": provider class}`` of every installed driver."""
    found: dict[str, type[InfraProvider]] = {}
    for entry in entry_points(group="sandloom.drivers"):
        loaded = entry.load()
        if isinstance(loaded, type) and issubclass(loaded, InfraProvider):
            found[entry.name] = loaded
    return found


def _url_option(provider: type[InfraProvider]) -> str | None:
    """The url option of ``provider``'s schema, ``None`` when it has none."""
    schema = provider.options_schema
    if schema is None:
        return None
    fields = schema.model_fields
    return next((option for option in _URL_OPTIONS if option in fields), None)


def _gaps(
    installed: Mapping[str, type[InfraProvider]], table: Mapping[str, str]
) -> list[str]:
    drivers = {key.partition(".")[2] for key in installed}
    gaps = []
    for key, cls in sorted(installed.items()):
        if cls.__module__.partition(".")[0] not in _REPOSITORY_PACKAGES:
            continue
        option = _url_option(cls)
        driver = key.partition(".")[2]
        if option is not None and table.get(driver) != option:
            gaps.append(
                f"{key}: has the option `{option}` but `{driver}` has no entry "
                "(or another option) in _URL_OPTION"
            )
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
