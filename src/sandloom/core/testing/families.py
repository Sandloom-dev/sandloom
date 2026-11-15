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

Until the Redis family moves into ``sandloom-redis``, the kernel package
registers it (``packages/sandloom/pyproject.toml``,
``sandloom.core.testing.redis_family:FAMILY``); the package then registers the
same name instead. An install made before the entry point existed has no
family: :func:`require_for_installed` refuses that (the pytest plugin calls it
in ``pytest_configure``, so a downstream suite gets the refusal too). Two
registrations of one name (a stale kernel entry beside the package's) are
refused as well, naming both distributions.

Imports nothing from the framework (safe before the settings snapshot).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from importlib import metadata
from typing import Protocol, runtime_checkable

#: Entry-point group: ``"<family>" = "pkg.testing:FAMILY"``.
TESTING_GROUP = "sandloom.testing"
#: The flat settings naming a cache / broker the harness may need to isolate.
FLAT_VARS = ("CACHE_URL", "BROKER_URL")
#: The setting declaring named infrastructure instances (JSON).
INFRA_VAR = "INFRA"


class HarnessFamilyError(RuntimeError):
    """A family cannot be loaded or does not implement the hooks."""


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
    "FLAT_VARS",
    "INFRA_VAR",
    "TESTING_GROUP",
    "HarnessFamily",
    "HarnessFamilyError",
    "harness_families",
    "require_for_installed",
]
