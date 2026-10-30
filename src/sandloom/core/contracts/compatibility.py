"""Pure contract compatibility shared by authoring, runtime and offline tools."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

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
