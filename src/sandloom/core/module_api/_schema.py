"""The wire-portable shape of a contract: :class:`ContractMethodRuntime`
(bind / validate / describe) and :class:`ContractSchema`."""

from __future__ import annotations

import functools
import inspect
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

import sandloom.core._internal.db_provider as _db_provider
from sandloom.core import contract_wire
from sandloom.core.module_api._errors import ContractArgumentError
from sandloom.core.module_api._spec import (
    SESSION_PARAM_NAMES,
    contract_spec,
)

_PARAM_KIND_NAMES: dict[Any, str] = {
    inspect.Parameter.POSITIONAL_ONLY: "positional_only",
    inspect.Parameter.POSITIONAL_OR_KEYWORD: "positional_or_keyword",
    inspect.Parameter.KEYWORD_ONLY: "keyword_only",
}


@dataclass(frozen=True)
class ContractMethodRuntime:
    """The validated, wire-portable shape of one exposed contract method.

    Built once at registration; the local path and the RPC server bind,
    validate and encode every call through it, so both see the same argument
    rules and the same result types.
    """

    name: str
    kind: Literal["read", "command"]
    #: Caller-facing signature (no ``self``/``cls``).
    signature: inspect.Signature
    annotations: dict[str, Any]
    returns: Any
    idempotency_key: str | None = None
    idempotency_scope: tuple[str, ...] = ()
    risk: str | None = None

    def describe(self) -> dict[str, Any]:
        """JSON-able schema published by the RPC describe endpoint."""
        description: dict[str, Any] = {
            "kind": self.kind,
            "idempotency_key": self.idempotency_key,
            "params": [
                {
                    "name": name,
                    "kind": _PARAM_KIND_NAMES.get(param.kind, "positional_or_keyword"),
                    "required": param.default is inspect.Parameter.empty,
                    "type": contract_wire.type_descriptor(self.annotations[name]),
                }
                for name, param in self.signature.parameters.items()
            ],
            "returns": contract_wire.type_descriptor(self.returns),
        }
        if self.idempotency_scope:
            description["idempotency_scope"] = list(self.idempotency_scope)
        if self.risk is not None:
            description["risk"] = self.risk
        return description

    @functools.cached_property
    def param_plans(self) -> dict[str, contract_wire.WirePlan]:
        """Per-parameter wire plan (codec choice, passthrough types)."""
        return {
            name: contract_wire.wire_plan(annotation)
            for name, annotation in self.annotations.items()
        }

    @functools.cached_property
    def result_plan(self) -> contract_wire.WirePlan:
        """Wire plan of the return annotation."""
        return contract_wire.wire_plan(self.returns)

    @functools.cached_property
    def params_typed(self) -> bool:
        """Every parameter is fully typed (an RPC request may use codec v2)."""
        return all(plan.typed for plan in self.param_plans.values())

    def bind(
        self,
        label: str,
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
        *,
        local: bool = False,
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Bind + validate UNTAGGED arguments exactly like a Python call would.

        Returns ``(positional_only_args, keyword_args)``. Unknown / missing
        arguments, positional-only or keyword-only violations and values that
        do not validate against their annotation raise
        :class:`ContractArgumentError` (422) before the provider runs.

        ``local=True`` binds the caller's ORIGINAL values of an in-process
        call: each goes through its parameter's :class:`WirePlan` (immutable
        exact-type values pass through, fully typed ones round-trip through
        pydantic-core, the rest through the tagged tree) — what the provider
        would receive over RPC.
        """
        try:
            bound = self.signature.bind(*args, **kwargs)
        except TypeError as exc:
            raise ContractArgumentError(f"{label}: {exc}") from exc
        positional: list[Any] = []
        keywords: dict[str, Any] = {}
        errors: list[dict[str, Any]] = []
        plans = self.param_plans if local else None
        for name, value in bound.arguments.items():
            try:
                if plans is not None:
                    try:
                        validated = plans[name].local(value)
                    except contract_wire.ContractWireError as exc:
                        raise ContractArgumentError(f"{label}: {exc}") from exc
                else:
                    validated = contract_wire.validate(value, self.annotations[name])
            except ValidationError as exc:
                errors.extend(
                    {
                        "loc": [name, *err.get("loc", ())],
                        "msg": err.get("msg"),
                        "type": err.get("type"),
                    }
                    for err in exc.errors()
                )
                continue
            if (
                self.signature.parameters[name].kind
                is inspect.Parameter.POSITIONAL_ONLY
            ):
                positional.append(validated)
            else:
                keywords[name] = validated
        if errors:
            raise ContractArgumentError(
                {"message": f"{label}: invalid arguments", "errors": errors}
            )
        return tuple(positional), keywords

    def decode_result(self, wire: Any, codec: int = contract_wire.CODEC_TAGGED) -> Any:
        """Rebuild a result from its wire form using the return annotation."""
        try:
            return self.result_plan.decode(wire, codec)
        except ValidationError as exc:
            raise contract_wire.ContractWireError(
                f"result of {self.name!r} does not match its return annotation: {exc}"
            ) from exc

    def local_result(self, label: str, result: Any) -> Any:
        """What an in-process caller receives for ``result`` (see bind(local=))."""
        return self._local_result_and_wire(label, result)[0]

    def _local_result_and_wire(self, label: str, result: Any) -> tuple[Any, Any]:
        """``(local_result(result), its typed wire form | None)``.

        The wire is what the caller's copy was rebuilt from (codec v2); a read
        scope's memo keeps it (:meth:`_memo_entry`) instead of another copy.
        """
        try:
            return self.result_plan.local_and_wire(result)
        except ValidationError as exc:
            raise contract_wire.ContractWireError(
                f"result of {self.name!r} does not match its return annotation: {exc}"
            ) from exc
        except contract_wire.ContractWireError as exc:
            raise contract_wire.ContractWireError(
                f"{label} returned a non-wire value: {exc}"
            ) from exc

    def _memo_entry(self, label: str, local: Any, wire: Any) -> Any:
        """What a read scope's memo keeps for a result: never an object a
        caller holds — the typed wire form when there is one, else a copy."""
        if wire is not None:
            return _MemoWire(wire)
        return self.local_result(label, local)

    def _memo_result(self, label: str, memo: Any) -> Any:
        """A caller's copy of a memoised result (:meth:`_memo_entry`)."""
        if type(memo) is _MemoWire:
            return self.result_plan.from_typed_wire(memo.wire)
        return self.local_result(label, memo)


class _MemoWire:
    """A memoised read result kept in its typed wire form."""

    __slots__ = ("wire",)

    def __init__(self, wire: Any) -> None:
        self.wire = wire


@dataclass(frozen=True)
class ContractSchema:
    """Every exposed method of one registered contract provider + a stable hash."""

    methods: dict[str, ContractMethodRuntime]
    hash: str

    def describe(self) -> dict[str, Any]:
        return {
            "hash": self.hash,
            "methods": {name: m.describe() for name, m in sorted(self.methods.items())},
        }


def _method_runtime(
    name: str, attr: Any, problems: list[str]
) -> ContractMethodRuntime | None:
    spec = contract_spec(attr)
    if spec is None:
        problems.append(
            f"public member {name!r} is neither a @contract_method nor private "
            "(prefix it with '_' to keep it off the contract)"
        )
        return None
    kind = spec.kind
    try:
        signature = inspect.signature(attr)
        hints = typing.get_type_hints(attr, include_extras=True)
    except Exception as exc:  # noqa: BLE001 - unresolvable annotations
        problems.append(f"{name}: annotations cannot be resolved ({exc})")
        return None
    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    ok = True
    for pname, param in signature.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            problems.append(
                f"{name}: *args / **kwargs are not allowed on a contract method"
            )
            ok = False
            continue
        annotation = hints.get(pname, inspect.Parameter.empty)
        if pname in SESSION_PARAM_NAMES or _db_provider.is_relational_session_type(
            annotation
        ):
            problems.append(
                f"{name}: declares a session parameter {pname!r}; use "
                "contract_session() inside the method"
            )
            ok = False
            continue
        problem = contract_wire.annotation_problem(annotation)
        if problem is not None:
            problems.append(f"{name}({pname}): {problem}")
            ok = False
            continue
        annotations[pname] = annotation
        params.append(param.replace(annotation=annotation))
    returns = hints.get("return", inspect.Parameter.empty)
    problem = contract_wire.annotation_problem(returns)
    if problem is not None:
        problems.append(f"{name}(return): {problem}")
        ok = False
    if not ok:
        return None
    return ContractMethodRuntime(
        name=name,
        kind=kind,
        signature=signature.replace(parameters=params, return_annotation=returns),
        annotations=annotations,
        returns=returns,
        idempotency_key=spec.idempotency_key,
        idempotency_scope=spec.idempotency_scope,
        risk=spec.risk,
    )
