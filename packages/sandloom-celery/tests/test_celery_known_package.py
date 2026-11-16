"""The kernel's table of driver packages (the install hint of SANDLOOM-B037)
agrees with the entry points this distribution publishes."""

from __future__ import annotations

from importlib import metadata

from sandloom.core.drivers.bootstrap import KNOWN_DRIVER_PACKAGES


def test_the_known_packages_are_the_entry_points_this_package_publishes() -> None:
    published = {
        entry.name
        for entry in metadata.entry_points(group="sandloom.drivers")
        if entry.dist is not None
        and entry.dist.name.replace("_", "-") == "sandloom-celery"
    }
    known = {
        f"{capability}.{driver}"
        for (capability, driver), (package, install) in KNOWN_DRIVER_PACKAGES.items()
        if package == "sandloom_celery" and install == "pip install sandloom-celery"
    }
    assert known
    assert known == published
