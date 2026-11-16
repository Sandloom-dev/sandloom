"""The test harness's side of a backend's family: what a driver package implements.

A package that ships a backend gives the test harness that backend's half of
the parallel-run isolation: an object with the :class:`HarnessFamily` hooks,
registered in the entry-point group ``sandloom.testing``
(``"<name>" = "package.testing:FAMILY"``; never a ``pytest11`` plugin, so
installing a package changes no unrelated suite). The kernel's harness calls the
hooks when it rewrites a worker's settings, claims a run's resources, allocates
or caps workers, and empties a test database. ``sandloom-redis`` is the model.

What the hooks need of the kernel is here, so a family imports nothing but
``sandloom.driverkit``:

* ownership of a backend's own resources: :func:`check_holder` refuses (or,
  under ``SANDLOOM_TEST_TAKE_OWNERSHIP=1``, takes over) a resource another run
  marked; :func:`current_owner` is this run's owner id; :func:`already_claimed`
  and :func:`record_claim` skip a resource the process verified already;
  :class:`TestResourceOwnershipError` is the refusal;
* the settings that name a backend: :data:`FLAT_VARS` (``CACHE_URL``,
  ``BROKER_URL``), :data:`INFRA_VAR`, :func:`parse_infra` and the options of an
  instance that hold a URL (:data:`INSTANCE_URL_OPTIONS`);
* the worker layout (:mod:`sandloom.driverkit.testing_isolation`) and what the
  drivers of the configured instances say a worker uses
  (:func:`isolated_instance_options`, the driver a composition selects:
  :func:`default_driver`);
* listing and pruning stale resources apart from the databases the kernel
  lists: :class:`Resource` and :func:`verdict`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sandloom.core.testing.families import (
    FLAT_VARS,
    INFRA_VAR,
    INSTANCE_URL_OPTIONS,
    HarnessFamily,
    parse_infra,
)
from sandloom.core.testing.ownership import (
    OWNER_ENV,
    TAKEOVER_ENV,
    TestResourceOwnershipError,
    already_claimed,
    check_holder,
    current_owner,
    record_claim,
)
from sandloom.core.testing.resources import Resource, verdict


def default_driver(capability: str, name: str) -> str | None:
    """The driver the composition root selects for instance ``capability/name``
    (its package's ``DEFAULT_INFRA``); ``None`` when it selects none."""
    # Imported on call: the harness loads a family before the settings
    # snapshot, and the seams import the composition and the registry.
    from sandloom.core.testing import family_seams

    return family_seams.default_driver(capability, name)


def isolated_instance_options(
    capability: str,
    name: str,
    driver: str,
    options: Mapping[str, Any],
    worker_id: str,
) -> Mapping[str, Any]:
    """What ``driver`` says worker ``worker_id``'s copy of instance
    ``capability/name`` uses (``InfraProvider.test_isolation``); ``options``
    unchanged for a plain factory, which has no class to ask. Nothing is
    built or opened."""
    from sandloom.core.testing import family_seams

    return family_seams.isolated_instance_options(
        capability, name, driver, options, worker_id
    )


__all__ = [
    "FLAT_VARS",
    "INFRA_VAR",
    "INSTANCE_URL_OPTIONS",
    "OWNER_ENV",
    "TAKEOVER_ENV",
    "HarnessFamily",
    "Resource",
    "TestResourceOwnershipError",
    "already_claimed",
    "check_holder",
    "current_owner",
    "default_driver",
    "isolated_instance_options",
    "parse_infra",
    "record_claim",
    "verdict",
]
