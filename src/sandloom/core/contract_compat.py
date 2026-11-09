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
* **Runtime** (ADR-0013 decisions 3 and 4) — at startup a caller resolves
  what it expects of every remote-bound contract: the expectation its own
  distribution ships beside the stub (``<module>/_generated/contracts/
  <stem>.describe.json``, written by ``sandloom generate``), else
  ``CONTRACT_SNAPSHOT_PATHS``, else the snapshot in the provider's package;
  the kernel families ``<m>.claims.v1`` / ``<m>.facts.v1`` are held to the
  kernel's own schema. The readiness probe compares each with the provider's
  live describe document (``GET /_sandloom/rpc/<contract>``): an incompatible
  provider fails ``/ready`` (``CONTRACT_COMPAT_CHECK``). A required remote
  contract with no expectation is never a warning: ``strict`` refuses
  startup, ``ready`` fails ``/ready`` with ``contract_unverified``.

The check is narrowed to the methods each consumer's composed ``consumes``
lists (``ContractRequirement(name, methods=(...))``), so an unrelated change
elsewhere in the contract never blocks its readiness.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sandloom.core.descriptors.generated_provenance as _provenance
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
    #: The consuming module whose copy this is (``None``: a snapshot or the
    #: kernel's own schema of a kernel contract family).
    consumer: str | None = None


# --------------------------------------------------------------------------- #
# Kernel contract families (ADR-0013 decision 4)
# --------------------------------------------------------------------------- #
class KernelClaimsV1:
    """The kernel-defined shape of every ``<m>.claims.v1`` contract."""

    @_module_api.contract_method
    async def contribute(self, user_id: Any) -> dict[str, Any]:
        """Merged claims of the owner's contributors for ``user_id``."""
        raise NotImplementedError


class KernelFactsV1:
    """The kernel-defined shape of every ``<m>.facts.v1`` contract."""

    @_module_api.contract_method
    async def describe(self) -> list[dict[str, Any]]:
        """Wire-safe descriptors of every access fact the owner holds."""
        raise NotImplementedError

    @_module_api.contract_method
    async def evaluate(
        self,
        name: str,
        subject: dict[str, Any],
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The owner's authoritative answer for fact ``name`` and ``subject``."""
        raise NotImplementedError


#: Family (second segment of ``<m>.<family>.v1``) -> its kernel-defined shape
#: (an instance: what ``contract_schema`` describes).
KERNEL_FAMILIES: dict[str, object] = {
    "claims": KernelClaimsV1(),
    "facts": KernelFactsV1(),
}


def kernel_family(contract: str) -> object | None:
    """The kernel shape of ``<m>.claims.v1`` / ``<m>.facts.v1``, else ``None``.

    These contracts have one shape for every owner: they are compared against
    it, never against a consumer copy (ADR-0013 decision 4).
    """
    parts = contract.split(".")
    if len(parts) != 3 or parts[2] != "v1" or not parts[0]:
        return None
    return KERNEL_FAMILIES.get(parts[1])


# --------------------------------------------------------------------------- #
# Expectations: beside the consumer's stub, CONTRACT_SNAPSHOT_PATHS, provider
# --------------------------------------------------------------------------- #
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


@dataclass(frozen=True)
class _Use:
    """One loaded module's ``consumes`` entry for a contract."""

    module: str
    methods: tuple[str, ...] | None
    required: bool


def _consumers(core: Any) -> dict[str, list[_Use]]:
    """Contract -> the loaded modules consuming it (in composition order)."""
    uses: dict[str, list[_Use]] = {}
    for desc in getattr(core, "module_descriptors", None) or ():
        module = str(getattr(desc, "name", "") or "")
        for req in getattr(desc, "consumes", None) or ():
            methods = tuple(sorted(set(getattr(req, "methods", ()) or ())))
            uses.setdefault(str(getattr(req, "name", "")), []).append(
                _Use(module, methods or None, bool(getattr(req, "required", True)))
            )
    return uses


def _union(uses: Iterable[_Use]) -> tuple[str, ...] | None:
    """The methods ``uses`` call together (``None``: one of them calls any)."""
    out: set[str] = set()
    for use in uses:
        if use.methods is None:
            return None
        out |= set(use.methods)
    return tuple(sorted(out))


def expectation_path(module: str, contract: str) -> Path | None:
    """``<module>/_generated/contracts/<stem>.describe.json``, located without
    importing the module (``None`` when its package is not found)."""
    directory = module_package_dir(module)
    if directory is None:
        return None
    stem = "_".join(contract.split("."))
    return (
        directory
        / _provenance.GENERATED
        / "contracts"
        / f"{stem}{_provenance.EXPECTATION_SUFFIX}"
    )


def read_expectation(path: Path, contract: str) -> tuple[dict[str, Any] | None, str]:
    """``(document, "")`` of a consumer's expectation file, else ``(None, why)``.

    The file must be what ``sandloom generate`` wrote: its digest verifies, it
    names ``contract`` and carries a describe document.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{path}: unreadable ({exc.__class__.__name__})"
    problem = _provenance.verify_expectation(text)
    if problem is not None:
        return None, f"{path}: {problem}; regenerate it with `sandloom generate`"
    doc = json.loads(text)
    describe = doc.get("describe")
    if doc.get("contract") != contract or not (
        isinstance(describe, dict) and isinstance(describe.get("methods"), dict)
    ):
        return None, f"{path}: not an expectation of {contract}"
    return doc, ""


@dataclass(frozen=True)
class ExpectationPlan:
    """What the compatibility check holds remote contracts to.

    ``expectations``: contract -> what each consumer (or a snapshot, or the
    kernel's schema) expects of it. ``unverified``: a required remote
    contract that cannot be checked -> why (``strict`` refuses startup with
    it, ``ready`` fails ``/ready`` with ``contract_unverified``).
    """

    expectations: dict[str, tuple[Expectation, ...]]
    unverified: dict[str, list[str]]

    def first(self) -> dict[str, Expectation]:
        """The first expectation of each contract."""
        return {name: found[0] for name, found in self.expectations.items() if found}


class _Resolver:
    """Resolves the expectations of remote contracts (startup only)."""

    def __init__(self, core: Any) -> None:
        self.core = core
        self.uses = _consumers(core)
        settings_obj = getattr(core, "settings", None)
        self.explicit: dict[str, tuple[dict[str, Any], str]] = {}
        for raw in list(getattr(settings_obj, "contract_snapshot_paths", None) or ()):
            try:
                for name, describe in load_snapshot_contracts(raw).items():
                    self.explicit.setdefault(name, (describe, str(raw)))
            except (OSError, ValueError) as exc:
                logger.warning("Unreadable contract snapshot %s: %s", raw, exc)
        self.cache: dict[Path, dict[str, dict[str, Any]]] = {}

    def snapshot(self, name: str) -> tuple[dict[str, Any], str] | None:
        """``CONTRACT_SNAPSHOT_PATHS``, then the provider package's snapshot."""
        if name in self.explicit:
            return self.explicit[name]
        for package in _candidate_packages(self.core, name):
            path = snapshot_path(package)
            if path is None or not path.is_file():
                continue
            if path not in self.cache:
                try:
                    self.cache[path] = load_snapshot_contracts(path)
                except (OSError, ValueError) as exc:
                    logger.warning("Unreadable contract snapshot %s: %s", path, exc)
                    self.cache[path] = {}
            describe = self.cache[path].get(name)
            if describe is not None:
                return describe, str(path)
        return None

    def resolve(self, name: str) -> tuple[tuple[Expectation, ...], list[str]]:
        """``(expectations, problems)`` of one remote contract."""
        uses = self.uses.get(name, [])
        family = kernel_family(name)
        if family is not None:
            describe = _strip(_module_api.contract_schema(family).describe())
            source = f"kernel schema of <m>.{name.split('.')[1]}.v1"
            methods = _union(uses) if uses else None
            return (Expectation(describe, source, methods),), []
        found: list[Expectation] = []
        problems: list[str] = []
        uncovered: list[_Use] = []
        for use in uses:
            expectation, why = self._beside(name, use)
            if expectation is not None:
                found.append(expectation)
                continue
            uncovered.append(use)
            if why and use.required:
                problems.append(why)  # a copy that does not verify is loud
        if uncovered or not uses:
            snapshot = self.snapshot(name)
            if snapshot is not None:
                methods = _union(uncovered) if uses else None
                found.append(Expectation(snapshot[0], snapshot[1], methods))
            elif not uses or any(u.required for u in uncovered):
                problems.append(self._missing(name, uncovered))
        return tuple(found), [*problems, *self._undescribed(name, found)]

    def _beside(self, name: str, use: _Use) -> tuple[Expectation | None, str]:
        """The expectation ``use.module`` ships beside its stub of ``name``."""
        path = expectation_path(use.module, name)
        if path is None or not path.is_file():
            return None, ""
        doc, why = read_expectation(path, name)
        if doc is None:
            return None, why
        methods = use.methods
        if methods is None and isinstance(doc.get("methods"), list):
            methods = tuple(sorted(str(m) for m in doc["methods"]))
        return Expectation(doc["describe"], str(path), methods, use.module), ""

    @staticmethod
    def _missing(name: str, uncovered: list[_Use]) -> str:
        who = ", ".join(sorted({u.module for u in uncovered if u.required}))
        consumer = f"consumed by {who}" if who else "bound in SERVICE_BINDINGS"
        return (
            f"{name} ({consumer}): no expectation beside the consumer's stub, in "
            "CONTRACT_SNAPSHOT_PATHS or in the provider's package; run `sandloom "
            "generate` in the consumer (it writes <stem>.describe.json beside the "
            "stub), or declare the contract optional (Contract(REF, optional=True))"
        )

    @staticmethod
    def _undescribed(name: str, found: Iterable[Expectation]) -> list[str]:
        """A method a consumer declares that its own expectation does not
        describe (a stale copy: it started calling the method afterwards)."""
        out = []
        for exp in found:
            if exp.consumer is None:
                continue
            described = exp.describe.get("methods") or {}
            missing = sorted(set(exp.methods or ()) - set(described))
            if missing:
                out.append(
                    f"{name}: {exp.consumer} calls {', '.join(missing)}, which its "
                    f"expectation {exp.source} does not describe; regenerate it "
                    "with `sandloom generate`"
                )
        return out


#: The attribute a core keeps its resolved plan under.
_PLAN_ATTR = "_sandloom_contract_expectations"


def _plan_key(core: Any, names: tuple[str, ...]) -> tuple[Any, ...]:
    """What a plan depends on (computed without I/O)."""
    settings_obj = getattr(core, "settings", None)
    return (
        names,
        tuple(getattr(settings_obj, "contract_snapshot_paths", None) or ()),
        tuple(getattr(core, "loaded_modules", None) or ()),
        tuple(
            (str(getattr(d, "name", "")), tuple(getattr(d, "consumes", None) or ()))
            for d in getattr(core, "module_descriptors", None) or ()
        ),
    )


def resolve_expectations(core: Any, names: Iterable[str]) -> ExpectationPlan:
    """What the compatibility check holds each contract in ``names`` to.

    Lookup (ADR-0013 decision 3): beside each consuming module's stub
    (``<module>/_generated/contracts/<stem>.describe.json``, narrowed to the
    methods its composed ``consumes`` lists), then ``CONTRACT_SNAPSHOT_PATHS``,
    then the snapshot in the provider's package. The kernel families are held
    to the kernel's schema. A required contract with no expectation, or a
    consumer copy that does not verify, is ``unverified`` (decision 4).

    Resolved once per core and set of contracts: the startup check reads the
    files, and ``/ready`` reuses the plan without reading any.
    """
    wanted = tuple(sorted(set(names)))
    key = _plan_key(core, wanted)
    cached = getattr(core, _PLAN_ATTR, None)
    if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == key:
        hit: ExpectationPlan = cached[1]
        return hit
    resolver = _Resolver(core)
    expectations: dict[str, tuple[Expectation, ...]] = {}
    unverified: dict[str, list[str]] = {}
    for name in wanted:
        found, problems = resolver.resolve(name)
        if found:
            expectations[name] = found
        if problems:
            unverified[name] = problems
    plan = ExpectationPlan(expectations, unverified)
    with suppress(AttributeError):  # a core without attributes: per call
        setattr(core, _PLAN_ATTR, (key, plan))
    return plan


def expected_contracts(core: Any, names: Iterable[str]) -> dict[str, Expectation]:
    """The first expectation of each contract in ``names`` that has one
    (:func:`resolve_expectations` gives the lookup order)."""
    return resolve_expectations(core, names).first()


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
    "KERNEL_FAMILIES",
    "SNAPSHOT_FILENAME",
    "SNAPSHOT_FORMAT",
    "CompatReport",
    "Expectation",
    "ExpectationPlan",
    "KernelClaimsV1",
    "KernelFactsV1",
    "check_against",
    "compare_contract",
    "expectation_path",
    "expected_contracts",
    "kernel_family",
    "load_snapshot_contracts",
    "module_package_dir",
    "module_snapshots",
    "read_expectation",
    "render_snapshot",
    "resolve_expectations",
    "snapshot_path",
]
