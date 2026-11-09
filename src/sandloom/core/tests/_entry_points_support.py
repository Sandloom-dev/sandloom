"""Installed ``sandloom.modules`` entry points for kernel tests, without a
module installed.

The kernel reads a module's name and distribution from the entry points only
(:mod:`sandloom.core.descriptors.module_names`): ``sandloom_identity`` is the
module ``identity`` because its distribution says so, not because its import
path ends with it. :func:`installed_entry_points` puts the standard modules'
entry points in place of the installed ones, so a kernel test that names them
by path runs the same in a venv with no module package (the ``kernel-without-batteries``
lane, ``tools/ci/no_batteries.py``). Nothing is imported: only the metadata
is replaced.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from sandloom.core.descriptors import module_names


@dataclass(frozen=True)
class FakeEntryPoint:
    """One ``sandloom.modules`` entry point: ``name = value`` from ``dist_name``."""

    name: str
    value: str
    dist_name: str

    @property
    def dist(self) -> object:
        return type("Dist", (), {"name": self.dist_name})()


#: The entry points the standard modules' distributions declare.
STANDARD = (
    FakeEntryPoint("identity", "sandloom_identity", "sandloom-identity"),
    FakeEntryPoint("tenancy", "sandloom_tenancy", "sandloom-tenancy"),
    FakeEntryPoint("billing", "sandloom_billing", "sandloom-billing"),
)


@contextmanager
def installed_entry_points(
    *entry_points: FakeEntryPoint,
) -> Iterator[tuple[FakeEntryPoint, ...]]:
    """The installed entry points are ``entry_points`` (default
    :data:`STANDARD`) inside the block, and only those.

    Its own patch, undone and the cache forgotten on the way out, so the
    entry points never leak into the next test (the suite is shuffled).
    """
    installed = entry_points or STANDARD
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module_names, "_entry_points", lambda: installed, raising=True)
        module_names.refresh_installed_modules()
        try:
            yield installed
        finally:
            module_names.refresh_installed_modules()
