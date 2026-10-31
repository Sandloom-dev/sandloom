"""Contract version compatibility for rolling upgrades (snapshots + checks).

A sync contract called over internal RPC is a contract between two
INDEPENDENTLY deployed processes: during a rolling upgrade a caller built from
release N+1 may talk to a provider still running release N (or the reverse).
This module makes that compatibility explicit and checkable:

* **Snapshots** — each module ships ``contracts.snapshot.json`` next to its
  ``__init__.py``: the describe document (methods, parameter / return type
  descriptors, kind, idempotency key) of every contract it provides. It is
  generated with ``sandloom contracts snapshot --write`` and diffed in CI
  (``sandloom contracts snapshot --check``; ``sandloom contracts check --against``).
* **Comparison** — :func:`compare_contract` decides whether a provider's
  ACTUAL contract serves a caller that EXPECTS another version of it. Additive
  changes pass (a new method, a new optional parameter, an optional model
  field, a widened parameter type); breaking ones are reported precisely (a
  removed method, a removed / newly required parameter, a changed kind or
  idempotency key, an incompatible type, a reordered positional parameter).
* **Runtime** — a caller resolves the expected shape of every remote-bound
  contract from the snapshot shipped in the provider module's package (one
  codebase, so the caller's image carries the provider's snapshot of the same
  release) or ``CONTRACT_SNAPSHOT_PATHS``; the readiness probe compares it
  with the provider's live describe document (``GET /_sandloom/rpc/<contract>``).
  An incompatible provider fails ``/ready`` (``CONTRACT_COMPAT_CHECK``).

Expectation is per contract by default; a consumer narrows it to the methods
it calls with ``ContractRequirement(name, methods=(...))`` so an unrelated
change elsewhere in the contract never blocks its readiness.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sandloom.core.descriptors.module_names as _module_names
import sandloom.core.module_api as _module_api
from sandloom.core.contracts.compatibility import (
    CompatReport as CompatReport,
)
from sandloom.core.contracts.compatibility import (
    compare_contract as compare_contract,
)

logger = logging.getLogger("sandloom.contract_compat")

#: File name of a module's contract snapshot (next to its ``__init__.py``).
SNAPSHOT_FILENAME = "contracts.snapshot.json"
#: Snapshot document format version.
SNAPSHOT_FORMAT = 1


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
def _strip(describe: Mapping[str, Any]) -> dict[str, Any]:
    """A describe document without transport-only keys (``codecs``)."""
    return {k: v for k, v in describe.items() if k != "codecs"}


def module_snapshots(core: Any) -> dict[str, dict[str, Any]]:
    """``{module: snapshot document}`` for every loaded module providing contracts.

    A contract belongs to the module that registered it (auto-exposed
    ``<module>.claims.v1`` / ``.facts.v1`` included).
    """

    by_module: dict[str, dict[str, Any]] = {}
    for name in sorted(core.service_registry.list()):
        owner = core.contract_owner(name)
        if owner is None:
            continue
        impl = core.service_registry.get(name)
        doc = by_module.setdefault(
            owner,
            {"format": SNAPSHOT_FORMAT, "module": owner, "contracts": {}},
        )
        doc["contracts"][name] = _strip(_module_api.contract_schema(impl).describe())
    return by_module


def render_snapshot(doc: Mapping[str, Any]) -> str:
    """Canonical text of a snapshot document (stable diffs)."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def module_package_dir(module: str) -> Path | None:
    """Directory of package ``module`` WITHOUT importing it (parents only)."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


def snapshot_path(module: str) -> Path | None:
    directory = module_package_dir(module)
    return directory / SNAPSHOT_FILENAME if directory is not None else None


def load_snapshot_contracts(path: str | Path) -> dict[str, dict[str, Any]]:
    """Contracts of a snapshot file, or of every snapshot under a directory.

    Accepts a module snapshot (``{"contracts": {...}}``), a bare
    ``{contract: describe}`` mapping, or a directory (searched recursively
    for ``contracts.snapshot.json``).
    """
    target = Path(path)
    files = sorted(target.rglob(SNAPSHOT_FILENAME)) if target.is_dir() else [target]
    contracts: dict[str, dict[str, Any]] = {}
    for file in files:
        data = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{file}: not a contract snapshot")
        body = data.get("contracts", data)
        if not isinstance(body, dict):
            raise ValueError(f"{file}: 'contracts' must be an object")
        for name, describe in body.items():
            if isinstance(describe, dict) and "methods" in describe:
                contracts[str(name)] = describe
    return contracts


@dataclass(frozen=True)
class Expectation:
    """What a caller expects of a remote contract (and where it came from)."""

    describe: dict[str, Any]
    source: str
    methods: tuple[str, ...] | None = None


def _candidate_packages(core: Any, contract: str) -> list[str]:
    owner = contract.split(".", 1)[0]
    loaded = list(getattr(core, "loaded_modules", None) or ())
    loaded += [
        str(getattr(d, "name", ""))
        for d in getattr(core, "module_descriptors", None) or ()
    ]
    parents: list[str] = []
    for module in loaded:
        parent = module.rpartition(".")[0]
        if parent not in parents:
            parents.append(parent)
    # An installed module package named like the contract's owner
    # (``billing`` -> ``sandloom_billing``), then the loaded modules' siblings.
    installed = [m.path for m in _module_names.installed_modules() if m.name == owner]
    return [*installed, *(f"{p}.{owner}" if p else owner for p in parents)]


def _consumer_methods(core: Any) -> dict[str, tuple[str, ...] | None]:
    """Contract -> methods the loaded modules declare they call (None = all)."""
    narrowed: dict[str, tuple[str, ...] | None] = {}
    for desc in getattr(core, "module_descriptors", None) or ():
        for req in getattr(desc, "consumes", None) or ():
            methods = tuple(getattr(req, "methods", ()) or ())
            name = str(getattr(req, "name", ""))
            if name in narrowed and narrowed[name] is None:
                continue
            if not methods:
                narrowed[name] = None
            else:
                previous = narrowed.get(name) or ()
                narrowed[name] = tuple(sorted(set(previous) | set(methods)))
    return narrowed


def expected_contracts(core: Any, names: Iterable[str]) -> dict[str, Expectation]:  # noqa: C901
    """The expected shape of each contract in ``names`` (when one is known).

    ``CONTRACT_SNAPSHOT_PATHS`` win; otherwise the snapshot shipped in the
    provider module's package (located from the contract's first segment next
    to the loaded modules' packages, without importing it).
    """
    settings_obj = getattr(core, "settings", None)
    extra = list(getattr(settings_obj, "contract_snapshot_paths", None) or ())
    explicit: dict[str, tuple[dict[str, Any], str]] = {}
    for raw in extra:
        try:
            for name, describe in load_snapshot_contracts(raw).items():
                explicit.setdefault(name, (describe, str(raw)))
        except (OSError, ValueError) as exc:
            logger.warning("Unreadable contract snapshot %s: %s", raw, exc)
    narrowed = _consumer_methods(core)
    found: dict[str, Expectation] = {}
    cache: dict[Path, dict[str, dict[str, Any]]] = {}
    for name in names:
        if name in explicit:
            describe, source = explicit[name]
            found[name] = Expectation(describe, source, narrowed.get(name))
            continue
        for package in _candidate_packages(core, name):
            path = snapshot_path(package)
            if path is None or not path.is_file():
                continue
            if path not in cache:
                try:
                    cache[path] = load_snapshot_contracts(path)
                except (OSError, ValueError) as exc:
                    logger.warning("Unreadable contract snapshot %s: %s", path, exc)
                    cache[path] = {}
            describe_doc = cache[path].get(name)
            if describe_doc is not None:
                found[name] = Expectation(describe_doc, str(path), narrowed.get(name))
                break
    return found


def check_against(
    expectations: Mapping[str, Expectation],
    actual: Mapping[str, Mapping[str, Any] | None],
) -> list[CompatReport]:
    """Compare every expectation that has an ``actual`` describe document."""
    return [
        compare_contract(
            name,
            expectation.describe,
            actual[name],
            methods=expectation.methods,
        )
        for name, expectation in sorted(expectations.items())
        if name in actual
    ]


__all__ = [
    "SNAPSHOT_FILENAME",
    "SNAPSHOT_FORMAT",
    "CompatReport",
    "Expectation",
    "check_against",
    "compare_contract",
    "expected_contracts",
    "load_snapshot_contracts",
    "module_package_dir",
    "module_snapshots",
    "render_snapshot",
    "snapshot_path",
]
