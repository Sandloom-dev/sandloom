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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sandloom.core.descriptors.module_names as _module_names
import sandloom.core.module_api as _module_api

logger = logging.getLogger("sandloom.contract_compat")

#: File name of a module's contract snapshot (next to its ``__init__.py``).
SNAPSHOT_FILENAME = "contracts.snapshot.json"
#: Snapshot document format version.
SNAPSHOT_FORMAT = 1

_POSITIONAL = ("positional_only", "positional_or_keyword")


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
@dataclass
class CompatReport:
    """Outcome of comparing one contract: breaking ``errors`` + ``warnings``.

    ``warnings`` are changes that cannot be verified (e.g. a model whose peer
    publishes no field information): logged, never failing readiness.
    """

    contract: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def compatible(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "compatible": self.compatible,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# Type descriptors: "can a value of type `value` be accepted by `acceptor`?"
# --------------------------------------------------------------------------- #
def _members(desc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if desc.get("k") == "union":
        out: list[Mapping[str, Any]] = []
        for member in desc.get("of") or ():
            out.extend(_members(member))
        return out
    return [desc]


def _show(desc: Mapping[str, Any]) -> str:
    kind = desc.get("k")
    if kind == "scalar":
        return str(desc.get("t"))
    if kind in ("model", "enum"):
        return str(desc.get("ref", kind)).rpartition(":")[2] or str(kind)
    if kind == "union":
        return " | ".join(_show(m) for m in desc.get("of") or ())
    if kind in ("list", "set", "frozenset"):
        return f"{kind}[{_show(desc.get('item') or {'k': 'any'})}]"
    if kind == "tuple":
        if desc.get("variadic"):
            return f"tuple[{_show(desc.get('item') or {'k': 'any'})}, ...]"
        return f"tuple[{', '.join(_show(i) for i in desc.get('items') or ())}]"
    if kind == "dict":
        key = _show(desc.get("key") or {"k": "any"})
        return f"dict[{key}, {_show(desc.get('value') or {'k': 'any'})}]"
    if kind == "literal":
        return f"Literal{list(desc.get('values') or ())}"
    return str(kind)


def _scalar_accepts(acceptor: str, value: str) -> bool:
    if acceptor == value:
        return True
    # pydantic (lax) widens an int into a float / Decimal losslessly.
    return value == "int" and acceptor in ("float", "decimal")


def _literal_type(value: Any) -> str | None:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    return None


def _single_accepts(
    acceptor: Mapping[str, Any],
    value: Mapping[str, Any],
    path: str,
    report: CompatReport,
) -> bool | None:
    """``True`` / ``False``; ``None`` = undecidable (a warning was recorded)."""
    a_kind, v_kind = acceptor.get("k"), value.get("k")
    if a_kind == "any":
        return True
    if v_kind == "any":
        report.warnings.append(
            f"{path}: the other side is untyped (Any) where {_show(acceptor)} is "
            "expected — cannot verify"
        )
        return None
    if a_kind == "none" or v_kind == "none":
        return a_kind == v_kind
    accepts = _KIND_ACCEPTS.get(a_kind) if isinstance(a_kind, str) else None
    if accepts is None:
        return False
    return accepts(acceptor, value, v_kind, path, report)


def _scalar_kind_accepts(
    acceptor: Mapping[str, Any],
    value: Mapping[str, Any],
    v_kind: Any,
    path: str,
    report: CompatReport,
) -> bool | None:
    if v_kind == "scalar":
        return _scalar_accepts(str(acceptor.get("t")), str(value.get("t")))
    if v_kind in ("literal", "enum"):
        return all(
            (lt := _literal_type(v)) is not None
            and _scalar_accepts(str(acceptor.get("t")), lt)
            for v in value.get("values") or ()
        )
    return False


def _choice_kind_accepts(
    acceptor: Mapping[str, Any],
    value: Mapping[str, Any],
    v_kind: Any,
    path: str,
    report: CompatReport,
) -> bool | None:
    """``literal`` / ``enum`` acceptor: every value is one of its values."""
    if v_kind in ("literal", "enum"):
        allowed = list(acceptor.get("values") or ())
        return all(v in allowed for v in value.get("values") or ())
    return False


def _collection_kind_accepts(
    acceptor: Mapping[str, Any],
    value: Mapping[str, Any],
    v_kind: Any,
    path: str,
    report: CompatReport,
) -> bool | None:
    """``list`` / ``set`` / ``frozenset`` acceptor."""
    if v_kind not in ("list", "set", "frozenset", "tuple"):
        return False
    if v_kind == "tuple" and not value.get("variadic"):
        return all(
            _accepts(acceptor.get("item") or {"k": "any"}, item, path, report)
            for item in value.get("items") or ()
        )
    return _accepts(
        acceptor.get("item") or {"k": "any"},
        value.get("item") or {"k": "any"},
        f"{path}[]",
        report,
    )


def _tuple_kind_accepts(
    acceptor: Mapping[str, Any],
    value: Mapping[str, Any],
    v_kind: Any,
    path: str,
    report: CompatReport,
) -> bool | None:
    if v_kind != "tuple":
        return False
    if acceptor.get("variadic"):
        items = (
            [value.get("item") or {"k": "any"}]
            if value.get("variadic")
            else list(value.get("items") or ())
        )
        return all(
            _accepts(acceptor.get("item") or {"k": "any"}, i, f"{path}[]", report)
            for i in items
        )
    if value.get("variadic"):
        return False
    a_items, v_items = (
        list(acceptor.get("items") or ()),
        list(value.get("items") or ()),
    )
    return len(a_items) == len(v_items) and all(
        _accepts(a, v, f"{path}[{i}]", report)
        for i, (a, v) in enumerate(zip(a_items, v_items, strict=True))
    )


def _dict_kind_accepts(
    acceptor: Mapping[str, Any],
    value: Mapping[str, Any],
    v_kind: Any,
    path: str,
    report: CompatReport,
) -> bool | None:
    if v_kind != "dict":
        return False
    return _accepts(
        acceptor.get("key") or {"k": "any"},
        value.get("key") or {"k": "any"},
        f"{path}{{key}}",
        report,
    ) and _accepts(
        acceptor.get("value") or {"k": "any"},
        value.get("value") or {"k": "any"},
        f"{path}{{}}",
        report,
    )


def _model_kind_accepts(
    acceptor: Mapping[str, Any],
    value: Mapping[str, Any],
    v_kind: Any,
    path: str,
    report: CompatReport,
) -> bool | None:
    if v_kind != "model":
        return False
    return _model_accepts(acceptor, value, path, report)


#: acceptor kind -> ``(acceptor, value, value kind, path, report) -> verdict``
#: (an unknown acceptor kind accepts nothing).
_KIND_ACCEPTS: dict[
    str,
    Callable[
        [Mapping[str, Any], Mapping[str, Any], Any, str, CompatReport], bool | None
    ],
] = {
    "scalar": _scalar_kind_accepts,
    "literal": _choice_kind_accepts,
    "enum": _choice_kind_accepts,
    "list": _collection_kind_accepts,
    "set": _collection_kind_accepts,
    "frozenset": _collection_kind_accepts,
    "tuple": _tuple_kind_accepts,
    "dict": _dict_kind_accepts,
    "model": _model_kind_accepts,
}


def _model_accepts(
    acceptor: Mapping[str, Any],
    value: Mapping[str, Any],
    path: str,
    report: CompatReport,
) -> bool | None:
    if acceptor.get("schema_hash") and acceptor.get("schema_hash") == value.get(
        "schema_hash"
    ):
        return True
    a_fields, v_fields = acceptor.get("fields"), value.get("fields")
    if not isinstance(a_fields, Mapping) or not isinstance(v_fields, Mapping):
        report.warnings.append(
            f"{path}: model {_show(acceptor)} changed (schema hash differs) and "
            "one side publishes no field information — cannot verify"
        )
        return None
    ok = True
    for name, a_field in a_fields.items():
        v_field = v_fields.get(name)
        if v_field is None:
            if a_field.get("required"):
                report.errors.append(
                    f"{path}.{name}: required field missing on the other side"
                )
                ok = False
            continue
        if a_field.get("required") and not v_field.get("required"):
            report.errors.append(
                f"{path}.{name}: required here but optional on the other side "
                "(it may be omitted)"
            )
            ok = False
        if not _accepts(
            a_field.get("type") or {"k": "any"},
            v_field.get("type") or {"k": "any"},
            f"{path}.{name}",
            report,
        ):
            ok = False
    # Fields only the value side has are ignored by the acceptor (pydantic's
    # default ``extra="ignore"``): an added field is additive.
    return ok


def _accepts(
    acceptor: Mapping[str, Any],
    value: Mapping[str, Any],
    path: str,
    report: CompatReport,
) -> bool:
    """Whether every value of ``value``'s type is accepted by ``acceptor``.

    Records a precise error for a definite mismatch at the top call site
    (nested mismatches are reported where they are decided).
    """
    a_members = _members(acceptor)
    for member in _members(value):
        verdicts = []
        for candidate in a_members:
            scratch = CompatReport(report.contract)
            verdict = _single_accepts(candidate, member, path, scratch)
            verdicts.append((verdict, scratch))
            if verdict is True and not scratch.errors:
                report.warnings.extend(scratch.warnings)
                break
        else:
            undecided = [s for v, s in verdicts if v is None]
            if undecided:
                report.warnings.extend(undecided[0].warnings)
                continue
            nested = [s for v, s in verdicts if s.errors]
            if nested and len(a_members) == 1:
                report.errors.extend(nested[0].errors)
            else:
                report.errors.append(
                    f"{path}: {_show(member)} is not accepted by {_show(acceptor)}"
                )
            return False
    return True


# --------------------------------------------------------------------------- #
# Contract / method comparison
# --------------------------------------------------------------------------- #
def compare_contract(
    name: str,
    expected: Mapping[str, Any],
    actual: Mapping[str, Any] | None,
    *,
    methods: Iterable[str] | None = None,
) -> CompatReport:
    """Can a caller EXPECTING ``expected`` use a provider describing ``actual``?

    ``expected`` / ``actual`` are describe documents (``{"hash", "methods"}``).
    ``methods`` narrows the check to the methods the caller uses (default:
    every expected method). ``actual=None`` means the provider does not serve
    the contract at all.
    """
    report = CompatReport(name)
    if actual is None:
        report.errors.append(f"{name}: not served by the provider")
        return report
    if expected.get("hash") and expected.get("hash") == actual.get("hash"):
        return report  # identical describe documents
    exp_methods: Mapping[str, Any] = expected.get("methods") or {}
    act_methods: Mapping[str, Any] = actual.get("methods") or {}
    selected = sorted(exp_methods) if methods is None else sorted(set(methods))
    for method in selected:
        exp = exp_methods.get(method)
        act = act_methods.get(method)
        where = f"{name}.{method}"
        if exp is None:
            report.warnings.append(
                f"{where}: not in the expected snapshot (cannot verify)"
            )
            continue
        if act is None:
            report.errors.append(f"{where}: method removed by the provider")
            continue
        _compare_method(where, exp, act, report)
    return report


def _compare_method(  # noqa: C901
    where: str,
    exp: Mapping[str, Any],
    act: Mapping[str, Any],
    report: CompatReport,
) -> None:
    if exp.get("kind") != act.get("kind"):
        report.errors.append(
            f"{where}: kind changed {exp.get('kind')!r} -> {act.get('kind')!r} "
            "(read / command semantics differ)"
        )
    if exp.get("idempotency_key") != act.get("idempotency_key"):
        report.errors.append(
            f"{where}: idempotency key changed {exp.get('idempotency_key')!r} -> "
            f"{act.get('idempotency_key')!r}"
        )
    if exp.get("idempotency_scope", []) != act.get("idempotency_scope", []):
        report.errors.append(f"{where}: idempotency scope changed")
    exp_params = list(exp.get("params") or ())
    act_params = list(act.get("params") or ())
    act_by_name = {str(p.get("name")): p for p in act_params}
    exp_names = {str(p.get("name")) for p in exp_params}
    exp_positional = [p for p in exp_params if p.get("kind") in _POSITIONAL]
    act_positional = [p for p in act_params if p.get("kind") in _POSITIONAL]
    for param in exp_params:
        pname = str(param.get("name"))
        target = act_by_name.get(pname)
        at = f"{where}({pname})"
        if target is None:
            report.errors.append(f"{at}: parameter removed by the provider")
            continue
        if target.get("required") and not param.get("required"):
            report.errors.append(f"{at}: became required (callers may omit it)")
        if param.get("kind") in _POSITIONAL:
            if target.get("kind") == "keyword_only":
                report.errors.append(
                    f"{at}: became keyword-only (callers may pass it positionally)"
                )
            elif exp_positional.index(param) != _index_of(act_positional, pname):
                report.errors.append(
                    f"{at}: positional order changed (callers may pass it positionally)"
                )
        if target.get("kind") == "positional_only" and param.get("kind") != (
            "positional_only"
        ):
            report.errors.append(
                f"{at}: became positional-only (callers may pass it by keyword)"
            )
        _accepts(
            target.get("type") or {"k": "any"},
            param.get("type") or {"k": "any"},
            at,
            report,
        )
    for param in act_params:
        pname = str(param.get("name"))
        if pname not in exp_names and param.get("required"):
            report.errors.append(
                f"{where}({pname}): new required parameter (callers do not pass it)"
            )
    _accepts(
        exp.get("returns") or {"k": "any"},
        act.get("returns") or {"k": "any"},
        f"{where}(return)",
        report,
    )


def _index_of(params: list[Mapping[str, Any]], name: str) -> int:
    for index, param in enumerate(params):
        if param.get("name") == name:
            return index
    return -1


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
