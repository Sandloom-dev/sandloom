"""Harness families: the backend-specific halves of the test harness, as hooks.

The harness isolates what a parallel test run shares: one database per xdist
worker, and the cache / broker of a backend the kernel does not ship (Redis).
The PostgreSQL / SQLite half lives in :mod:`~sandloom.core.testing.workers`,
:mod:`~sandloom.core.testing.ownership`, :mod:`~sandloom.core.testing.slots` and
:mod:`~sandloom.core.testing.session_checks`; every other backend plugs in as a
*family*: an object with the :class:`HarnessFamily` hooks, which those modules
call and which no kernel module needs to know the backend for.

A family is registered in the entry-point group :data:`TESTING_GROUP`
(``sandloom.testing``, ``"<name>" = "package.module:FAMILY"``), the way a driver
is in ``sandloom.drivers``. It is deliberately not a ``pytest11`` plugin:
installing a package must never change the behaviour of an unrelated test
suite. The kernel loads the groups it finds when the first hook runs (a process
with no family is the DB-only harness, with nothing imported).

The ``redis`` family is the ``sandloom-redis`` package's (its ``pyproject.toml``
registers it); the kernel registers none. An install made before the entry
point moved there has no family, or a stale kernel entry beside the package's:
:func:`require_for_installed` refuses the first (the pytest plugin calls it in
``pytest_configure``, so a downstream suite gets the refusal too) and the
discovery refuses the second, naming both distributions. A family package
imports the kernel's side of the harness only through
``sandloom.driverkit.testing_family``.

Imports nothing from the framework (safe before the settings snapshot).
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterable, Mapping, MutableMapping
from importlib import metadata
from typing import Any, Protocol, runtime_checkable

#: Entry-point group: ``"<family>" = "pkg.testing:FAMILY"``.
TESTING_GROUP = "sandloom.testing"
#: The setting declaring named infrastructure instances (JSON).
INFRA_VAR = "INFRA"
#: The options of an ``INFRA`` instance that hold a service URL: ``url`` (the
#: cache, lease, atomic store and event bus drivers) and the task queue's
#: ``broker`` and ``backend``.
INSTANCE_URL_OPTIONS = ("url", "broker", "backend")


class HarnessFamilyError(RuntimeError):
    """A family cannot be loaded or does not implement the hooks."""


def parse_infra(raw: str | None) -> dict[str, dict[str, dict[str, Any]]]:
    """The ``INFRA`` setting as ``{capability: {instance: spec}}``; ``{}`` for
    a missing or malformed one (settings refuse it when they load)."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(cap): {
            str(name): spec
            for name, spec in instances.items()
            if isinstance(spec, dict)
        }
        for cap, instances in parsed.items()
        if isinstance(instances, dict)
    }


@runtime_checkable
class HarnessFamily(Protocol):
    """The hooks one backend gives the harness. Every hook is a no-op for a
    process that cannot reach the backend (no client installed, no URL set)."""

    @property
    def name(self) -> str:
        """The family's name: its entry-point name."""
        ...

    def rewrite_worker(
        self,
        values: Mapping[str, str | None],
        environ: Mapping[str, str],
        worker_id: str,
        number: int,
    ) -> dict[str, str]:
        """The replacements of xdist worker ``worker_id``'s settings in ``values``."""
        ...

    def claim(self, values: Mapping[str, str]) -> None:
        """Verify (or claim) every resource of ``values`` this family resets;
        raises ``TestResourceOwnershipError`` for another run's."""
        ...

    def slot_bases(
        self,
        urls: Mapping[str, str],
        owner: str,
        environ: MutableMapping[str, str],
    ) -> dict[str, str]:
        """Own base resources for a test-run slot of ``owner``: the new URL per
        setting of ``urls`` (``{}``: none claimed). May default settings of its
        own into ``environ``. Raises ``RuntimeError`` / ``OSError`` when the
        backend has no room (the run then reports it itself)."""
        ...

    def release_slot_bases(self, urls: Iterable[str], owner: str) -> None:
        """Hand back what :meth:`slot_bases` claimed (``urls`` of every family:
        ignore the ones that are not this family's)."""
        ...

    def allocate_workers(self, values: Mapping[str, str], count: int) -> None:
        """Claim resources for the ``count`` xdist workers before they start
        (the controller's ``pytest_sessionstart``), publishing the mapping in
        the environment the workers inherit."""
        ...

    def cap_workers(self, values: Mapping[str, str], count: int) -> int:
        """``count`` workers capped to what the backend has room for."""
        ...

    async def flush_owned(self, provider: object) -> bool:
        """Empty the test database behind ``provider`` (a cache provider the app
        resolves) when it is this family's and this run owns it; ``False`` when
        it is not this family's."""
        ...

    def owns_url(self, url: str) -> bool:
        """``url`` is one this family isolates (a URL of its backend): the
        harness's ``SANDLOOM_TEST_CACHE_URL`` / ``SANDLOOM_TEST_BROKER_URL``
        merge replaces only URLs a family owns."""
        ...

    def same_database(self, stated: str, base: str) -> bool:
        """``stated`` and ``base``, both URLs this family owns, name the same
        server and database (a URL that leaves the database out names the
        backend's default one)."""
        ...

    def parallel_env(self) -> dict[str, str]:
        """The environment an xdist (parallel) run of ``sandloom test`` sets for
        this family unless the caller already set the names; ``{}`` for a family
        that needs none."""
        ...


_loaded: tuple[HarnessFamily, ...] | None = None


def _as_family(loaded: object, what: str, target: str) -> HarnessFamily:
    if not isinstance(loaded, HarnessFamily):
        raise HarnessFamilyError(
            f"the harness family {what} ({target}) does not implement HarnessFamily "
            "(sandloom.core.testing.families): it needs a name and every hook"
        )
    return loaded


def _distribution(entry: metadata.EntryPoint) -> str:
    return (
        f"distribution {entry.dist.name!r}" if entry.dist else "an unknown distribution"
    )


def _discover() -> tuple[HarnessFamily, ...]:
    found: dict[str, HarnessFamily] = {}
    registered: dict[str, metadata.EntryPoint] = {}
    for entry in sorted(
        metadata.entry_points().select(group=TESTING_GROUP), key=lambda e: e.name
    ):
        # Before loading: a stale registration (a package that dropped the
        # module it points at) must be named as the duplicate it is, not fail
        # as an import error.
        earlier = registered.setdefault(entry.name, entry)
        if earlier is not entry:
            raise HarnessFamilyError(
                f"the harness family {entry.name!r} is registered twice in "
                f"{TESTING_GROUP!r}: by {_distribution(earlier)} "
                f"({earlier.value}) and by {_distribution(entry)} "
                f"({entry.value}); uninstall or reinstall the package that "
                "no longer owns it (`uv sync`)"
            )
        try:
            loaded = entry.load()
        except Exception as exc:  # any import failure is a broken family
            raise HarnessFamilyError(
                f"the harness family {entry.name!r} ({entry.value}) failed to "
                f"import: {exc!r}; fix or reinstall the package that publishes it"
            ) from exc
        family = _as_family(loaded, repr(entry.name), entry.value)
        if family.name != entry.name:
            raise HarnessFamilyError(
                f"the harness family registered as {entry.name!r} ({entry.value}) "
                f"names itself {family.name!r}"
            )
        found[entry.name] = family
    return tuple(found.values())


def harness_families() -> tuple[HarnessFamily, ...]:
    """Every family registered in the group, by name (discovered once)."""
    global _loaded
    if _loaded is None:
        _loaded = _discover()
    return _loaded


def client_installed(module: str) -> bool:
    """Whether the client library ``module`` can be imported. Looks for it; does
    not import it (an application on memory drivers has none, and needs none)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def require_for_installed(client_installed: Mapping[str, bool]) -> None:
    """Refuse a process that has a backend's client but not its family.

    ``client_installed`` maps a family name to whether its client can be
    imported (``{"redis": importlib.util.find_spec("redis") is not None}``).
    A run with the client and no family would silently share that backend
    between parallel workers and runs, so the harness stops instead.
    """
    if not any(client_installed.values()):
        return  # no client: nothing to isolate, and no family needs loading
    names = {family.name for family in harness_families()}
    for name, installed in client_installed.items():
        if installed and name not in names:
            raise HarnessFamilyError(
                f"the {name!r} client is installed but no harness family "
                f"{name!r} is registered (entry-point group {TESTING_GROUP!r}): "
                "parallel test workers would share one backend; (re)install the "
                "package that provides it (`uv sync`)"
            )


__all__ = [
    "INFRA_VAR",
    "INSTANCE_URL_OPTIONS",
    "TESTING_GROUP",
    "HarnessFamily",
    "HarnessFamilyError",
    "client_installed",
    "harness_families",
    "parse_infra",
    "require_for_installed",
]
