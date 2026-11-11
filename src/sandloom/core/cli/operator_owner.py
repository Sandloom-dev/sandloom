"""Who answers an operator command, confirmed before its first side effect.

An operator command (``sandloom security ...``, ``sandloom admin ...``) acts
on what a module owns. The kernel names no such module (ADR-0012 decision 1):
:func:`operator_owner` reads the CANDIDATE owner of a capability from the
composition root's claim-authority map, and the command asks that module's
reserved ``<owner>.security.v1`` (ADR-0013 decision 4: only module ``owner``
registers it, SANDLOOM-B034; no kernel shape, every contract rule applies).

* sessions, accounts, credentials and the email-to-user lookup: the map's
  ``issuer``;
* platform-administrator roles (grant, list, first-admin check): the
  authority of ``roles``;
* tenant member enumeration: the authority of ``tenant_ids``.

The map is read whole (:attr:`~sandloom.core.claims.ClaimAuthorities.verified`,
``declared_issuer``, ``names``), so a process of a split that does not load
the owner resolves the same owner as the one that does. The owner's public
name comes from the declaration (``Authority(name, module=path)``), an
installed entry point or a module loaded here, never from the last import
segment.

A claim authority is a candidate, not proof: :func:`confirm_operator_capability`
confirms each method by describe (the local schema in one process, the bound
provider's live describe in a split), compares each method's schema with
the shape the kernel calls it with (:data:`OPERATOR_METHOD_SHAPES`: an
incompatible method refuses like a missing one), checks that the binding
names the owner's own service and that the provider is the module the map
names, and refuses otherwise (SANDLOOM-B039 / SANDLOOM-B040, exit status 1)
before anything is done, a dry run included. A command then checks each
answer where it consumes it (:func:`answer_problem`) before its next side
effect. Operator paths only: nothing here runs at import or on a request.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

import sandloom.core.rpc.proxy as _proxy
import sandloom.core.rpc_identity as _rpc_identity
from sandloom.core.claims import ISSUER_ENTRY, security_contract_name
from sandloom.core.descriptors.module_names import module_name
from sandloom.core.module_api._service_registry import ServiceUnavailableInDeployment
from sandloom.core.rpc_transport import RpcResponseBindingError

#: The refusal of a capability that cannot be confirmed.
CODE = "SANDLOOM-B039"
#: The refusal of a binding or provider that is not the named owner.
SUBSTITUTION_CODE = "SANDLOOM-B040"

#: Operator capability -> the claim-map entry whose authority is its
#: candidate owner.
OPERATOR_CAPABILITIES: Mapping[str, str] = MappingProxyType(
    {
        "sessions": ISSUER_ENTRY,
        "accounts": ISSUER_ENTRY,
        "credentials": ISSUER_ENTRY,
        "user_lookup": ISSUER_ENTRY,
        "platform_admin": "roles",
        "tenant_members": "tenant_ids",
    }
)


def _scalar(name: str) -> dict[str, Any]:
    return {"k": "scalar", "t": name}


_NONE: dict[str, Any] = {"k": "none"}
_UUID = _scalar("uuid")
_STR = _scalar("str")
_INT = _scalar("int")
_BOOL = _scalar("bool")
_DATETIME = _scalar("datetime")


def _optional(shape: dict[str, Any]) -> dict[str, Any]:
    return {"k": "union", "of": [shape, _NONE]}


def _list(item: dict[str, Any]) -> dict[str, Any]:
    return {"k": "list", "item": item}


def _record(
    required: Mapping[str, Any], optional: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """A DTO the command reads: ``required`` fields it needs, ``optional``
    ones it reads when the owner has them (each of the stated type)."""
    return {"k": "record", "fields": dict(required), "optional": dict(optional or {})}


#: The kinds of session ``end_sessions`` may report.
SESSION_KIND_VALUES = ("access_tokens", "refresh_sessions", "provider_sessions")

_OPERATION = (_STR, _STR, _STR)  # reason, operator, operation key
_ACCOUNT = _record(
    {"user_id": _UUID, "email": _STR, "email_verified": _BOOL},
    {
        "is_active": _BOOL,
        "password_set": _BOOL,
        "last_login_at": _optional(_DATETIME),
    },
)


@dataclass(frozen=True)
class MethodShape:
    """What an operator command sends to an owner's method (positional
    parameter types) and what it reads from the answer (``returns``)."""

    params: tuple[dict[str, Any], ...]
    returns: dict[str, Any]

    def text(self) -> str:
        """``"(list[uuid], str, str, str) -> int"``."""
        params = ", ".join(render_shape(p) for p in self.params)
        return f"({params}) -> {render_shape(self.returns)}"


#: Operator method -> the shape the kernel's commands call it with. Compared
#: with the owner's described schema before the first side effect, and each
#: answer is checked against ``returns`` where it is consumed.
OPERATOR_METHOD_SHAPES: Mapping[str, MethodShape] = MappingProxyType(
    {
        "list_user_ids": MethodShape((_optional(_UUID), _INT), _list(_UUID)),
        "list_member_user_ids": MethodShape(
            (_UUID, _optional(_UUID), _INT), _list(_UUID)
        ),
        "end_sessions": MethodShape(
            (_list(_UUID), *_OPERATION),
            _record(
                {
                    "affected": _INT,
                    "ended": _list(
                        {"k": "literal", "values": list(SESSION_KIND_VALUES)}
                    ),
                }
            ),
        ),
        "require_password_reset": MethodShape((_list(_UUID), *_OPERATION), _INT),
        "account_by_email": MethodShape((_STR,), _optional(_ACCOUNT)),
        "accounts_by_id": MethodShape((_list(_UUID),), _list(_ACCOUNT)),
        "is_platform_admin": MethodShape((_UUID,), _BOOL),
        "platform_admin_ids": MethodShape((), _list(_UUID)),
        "platform_admin_exists": MethodShape((), _BOOL),
        "appoint_platform_admin": MethodShape((_UUID, *_OPERATION), _BOOL),
        "record_admin_listing": MethodShape((_STR, _STR, _BOOL, _STR), _BOOL),
        "password_problem": MethodShape((_STR, _STR), _optional(_STR)),
        "set_password_token": MethodShape((_UUID,), _optional(_STR)),
        "set_password_with_token": MethodShape((_STR, _STR, _STR), _BOOL),
        "create_platform_admin": MethodShape((_STR, _STR, *_OPERATION), _UUID),
        "take_over_platform_admin": MethodShape((_UUID, *_OPERATION), _BOOL),
        "reissue_set_password": MethodShape((_UUID, *_OPERATION), _BOOL),
    }
)


def _render_values(prefix: str, desc: Mapping[str, Any]) -> str:
    return f"{prefix}[{', '.join(repr(v) for v in desc.get('values') or ())}]"


def _render_record(desc: Mapping[str, Any]) -> str:
    fields = [f"{n}: {render_shape(t)}" for n, t in desc["fields"].items()]
    fields += [f"{n}?: {render_shape(t)}" for n, t in desc["optional"].items()]
    return "{" + ", ".join(fields) + "}"


def _render_model(desc: Mapping[str, Any]) -> str:
    found = desc.get("fields")
    if not isinstance(found, Mapping):
        return str(desc.get("ref") or "a model")
    fields = [
        f"{n}{'' if (f or {}).get('required', True) else '?'}: "
        f"{render_shape((f or {}).get('type'))}"
        for n, f in found.items()
    ]
    return "{" + ", ".join(fields) + "}"


_RENDER: Mapping[str, Any] = MappingProxyType(
    {
        "scalar": lambda d: str(d.get("t")),
        "none": lambda d: "None",
        "any": lambda d: "Any",
        "tuple": lambda d: "tuple",
        "union": lambda d: " | ".join(render_shape(m) for m in d.get("of") or ()),
        "list": lambda d: f"list[{render_shape(d.get('item'))}]",
        "set": lambda d: f"set[{render_shape(d.get('item'))}]",
        "frozenset": lambda d: f"frozenset[{render_shape(d.get('item'))}]",
        "literal": lambda d: _render_values("Literal", d),
        "enum": lambda d: _render_values("Enum", d),
        "dict": lambda d: (
            f"dict[{render_shape(d.get('key'))}, {render_shape(d.get('value'))}]"
        ),
        "record": _render_record,
        "model": _render_model,
    }
)


def render_shape(desc: Any) -> str:
    """A wire type descriptor (or an expected shape) as one line of text."""
    if not isinstance(desc, Mapping):
        return repr(desc)
    render = _RENDER.get(str(desc.get("k")))
    return str(desc.get("k")) if render is None else str(render(desc))


def _model_conforms(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    found = actual.get("fields")
    if not isinstance(found, Mapping):
        return False

    def field_type(name: str) -> Any:
        spec = found.get(name)
        return spec.get("type") if isinstance(spec, Mapping) else None

    if any(
        name not in found or not conforms(field_type(name), shape)
        for name, shape in expected["fields"].items()
    ):
        return False
    # An optional field the owner has may also be None (read as not stated).
    return all(
        conforms(field_type(name), _optional(shape))
        for name, shape in expected["optional"].items()
        if name in found
    )


def _literal_conforms(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    values = list(actual.get("values") or ())
    allowed = expected.get("values") or ()
    return bool(values) and all(v in allowed for v in values)


#: Same kind on both sides: whether ``actual`` is within ``expected``.
_SAME_KIND: Mapping[str, Any] = MappingProxyType(
    {
        "scalar": lambda a, e: a.get("t") == e.get("t"),
        "none": lambda a, e: True,
        "list": lambda a, e: conforms(a.get("item"), e.get("item")),
        "set": lambda a, e: conforms(a.get("item"), e.get("item")),
        "frozenset": lambda a, e: conforms(a.get("item"), e.get("item")),
        "literal": _literal_conforms,
    }
)


def conforms(actual: Any, expected: Any) -> bool:
    """Whether every value of the described type ``actual`` is a value of
    ``expected``: a provider's answer type against what a command reads (a
    narrower answer conforms), or what a command sends against a provider's
    parameter type (``conforms(sent, parameter)``)."""
    if not isinstance(actual, Mapping) or not isinstance(expected, Mapping):
        return False
    a_kind, e_kind = actual.get("k"), expected.get("k")
    if e_kind == "any" or a_kind == "any":
        return e_kind == "any"
    if a_kind == "union":
        members = actual.get("of") or ()
        return bool(members) and all(conforms(m, expected) for m in members)
    if e_kind == "union":
        return any(conforms(actual, m) for m in expected.get("of") or ())
    if e_kind == "record":
        return a_kind == "model" and _model_conforms(actual, expected)
    same = _SAME_KIND.get(str(a_kind)) if a_kind == e_kind else None
    return bool(same is not None and same(actual, expected))


def schema_problem(method: str, described: Any) -> str | None:
    """Why ``described`` (a method's describe schema: ``params``,
    ``returns``) is not what the kernel calls ``method`` with, or ``None``."""
    shape = OPERATOR_METHOD_SHAPES.get(method)
    if shape is None:
        raise ValueError(f"no expected shape for operator method {method!r}")
    params = described.get("params") if isinstance(described, Mapping) else None
    returns = described.get("returns") if isinstance(described, Mapping) else None
    if not isinstance(params, list) or returns is None:
        return f"expected {shape.text()}, got no schema"
    got = (
        "("
        + ", ".join(
            render_shape(p.get("type") if isinstance(p, Mapping) else p) for p in params
        )
        + f") -> {render_shape(returns)}"
    )
    fits = len(params) >= len(shape.params) and all(
        isinstance(p, Mapping)
        and p.get("kind") in ("positional_or_keyword", "positional_only")
        and conforms(sent, p.get("type"))
        for sent, p in zip(shape.params, params, strict=False)
    )
    extra_required = any(
        not isinstance(p, Mapping) or p.get("required", True)
        for p in params[len(shape.params) :]
    )
    if not fits or extra_required or not conforms(returns, shape.returns):
        return f"expected {shape.text()}, got {got}"
    return None


#: A scalar answer: whether ``value`` is of the described scalar type.
_SCALAR_OK: Mapping[str, Any] = MappingProxyType(
    {
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "bool": lambda v: isinstance(v, bool),
        "str": lambda v: isinstance(v, str),
        "uuid": lambda v: isinstance(v, uuid.UUID) or _parses(uuid.UUID, v),
        "datetime": lambda v: (
            isinstance(v, datetime) or _parses(datetime.fromisoformat, v)
        ),
    }
)


def _union_problem(value: Any, shape: Mapping[str, Any]) -> str | None:
    members = list(shape.get("of") or ())
    problems = [_value_problem(value, m) for m in members]
    if any(p is None for p in problems):
        return None
    stated = [p for m, p in zip(members, problems, strict=True) if m != _NONE]
    # ``X | None`` and not None: what is wrong with it as an X.
    return stated[0] if len(stated) == 1 else _wrong(value, shape)


def _scalar_problem(value: Any, shape: Mapping[str, Any]) -> str | None:
    check = _SCALAR_OK.get(str(shape.get("t")))
    return None if check is not None and check(value) else _wrong(value, shape)


def _list_problem(value: Any, shape: Mapping[str, Any]) -> str | None:
    if not isinstance(value, (list, tuple)):
        return _wrong(value, shape)
    for item in value:
        problem = _value_problem(item, shape["item"])
        if problem is not None:
            return problem
    return None


def _value_problem(value: Any, shape: Mapping[str, Any]) -> str | None:
    check = _VALUE_CHECKS.get(str(shape.get("k")))
    return _wrong(value, shape) if check is None else check(value, shape)


def _record_problem(value: Any, shape: Mapping[str, Any]) -> str | None:
    if value is None or isinstance(value, (str, int, float, list, tuple)):
        return _wrong(value, shape)
    missing = object()

    def read(name: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(name, missing)
        return getattr(value, name, missing)

    for name, field_shape in shape["fields"].items():
        found = read(name)
        if found is missing:
            return f"expected {render_shape(shape)}, got no {name!r} in {_kind(value)}"
        problem = _value_problem(found, field_shape)
        if problem is not None:
            return f"{name}: {problem}"
    for name, field_shape in shape["optional"].items():
        found = read(name)
        if found is missing or found is None:
            continue
        problem = _value_problem(found, field_shape)
        if problem is not None:
            return f"{name}: {problem}"
    return None


#: Answer kind -> its check (``None``: the answer is of that shape).
_VALUE_CHECKS: Mapping[str, Any] = MappingProxyType(
    {
        "union": _union_problem,
        "none": lambda v, s: None if v is None else _wrong(v, s),
        "scalar": _scalar_problem,
        "list": _list_problem,
        "literal": lambda v, s: None if v in (s.get("values") or ()) else _wrong(v, s),
        "record": lambda v, s: _record_problem(v, s),
    }
)


def _parses(parse: Any, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse(value)
    except ValueError:
        return False
    return True


def _kind(value: Any) -> str:
    return "None" if value is None else type(value).__name__


def _wrong(value: Any, shape: Mapping[str, Any]) -> str:
    return f"expected {render_shape(shape)}, got {_kind(value)}"


def answer_problem(method: str, answer: Any) -> str | None:
    """Why ``answer`` is not what ``method`` returns to the kernel's
    commands (:data:`OPERATOR_METHOD_SHAPES`), or ``None``. Strict: an
    ``int`` is not a ``bool`` nor a DTO, a list holds the stated items. A
    command checks each answer before its next side effect."""
    shape = OPERATOR_METHOD_SHAPES.get(method)
    if shape is None:
        raise ValueError(f"no expected shape for operator method {method!r}")
    return _value_problem(answer, shape.returns)


class OperatorCapabilityUnavailable(SystemExit):
    """SANDLOOM-B039: the owner of an operator capability cannot be confirmed
    to provide it (no owner, unknown name, contract neither local nor bound,
    a missing method, a missing ``RPC_ALLOWED_CALLS`` grant). A
    :class:`SystemExit` (status 1): the command stops before acting."""

    sandloom_code = CODE

    def __init__(self, message: str) -> None:
        super().__init__(f"error: {CODE}: {message} (`sandloom explain {CODE}`)")


class OperatorOwnerSubstitution(SystemExit):
    """SANDLOOM-B040: the service that would answer ``<owner>.security.v1``
    is not the owner the claim map names. A :class:`SystemExit` (status 1)."""

    sandloom_code = SUBSTITUTION_CODE

    def __init__(self, message: str) -> None:
        super().__init__(
            f"error: {SUBSTITUTION_CODE}: {message} "
            f"(`sandloom explain {SUBSTITUTION_CODE}`)"
        )


@dataclass(frozen=True)
class OperatorOwner:
    """The candidate owner of one operator capability.

    ``command``: the operator command (refusals name it); ``key``: the
    claim-map entry it was read from; ``module``: the owner's public name;
    ``path``: its import path.
    """

    command: str
    capability: str
    key: str
    module: str
    path: str

    @property
    def contract(self) -> str:
        """The owner's operator contract, ``<module>.security.v1``."""
        return security_contract_name(self.module)

    def label(self) -> str:
        """``"owner 'issuer' = acme_idp (oidcapp.modules.acme_idp)"``."""
        return f"owner {self.key!r} = {self.module} ({self.path})"


@dataclass(frozen=True)
class ConfirmedContract:
    """An owner's operator contract, confirmed (:func:`confirm_operator_contract`).

    ``contract``: what to call (the local bound provider or the remote
    proxy); ``schemas``: the confirmed methods' described schemas;
    ``refresh_sessions``: the owner's ``refresh_sessions`` declaration, from
    its descriptor here or its live describe in a split (``None``: not
    stated, e.g. a provider older than the describe key).
    """

    contract: Any
    schemas: Mapping[str, Any] = field(default_factory=dict)
    refresh_sessions: bool | None = None


def _loaded_paths(core: Any) -> set[str]:
    return {
        str(name)
        for name in (getattr(d, "name", None) for d in core.module_descriptors)
        if name
    }


def _assignment_fix(key: str) -> str:
    return (
        f"assign it in the composition root: create_app(claim_authorities="
        f"{{{key!r}: Authority('<name>', module='<import path>'), ...}})"
    )


def operator_owner(core: Any, capability: str, *, command: str) -> OperatorOwner:
    """The candidate owner of ``capability`` (:data:`OPERATOR_CAPABILITIES`)
    in ``core``'s composition, else :class:`OperatorCapabilityUnavailable`.

    Reads the root's whole claim-authority declaration; no I/O. The owner is
    not yet confirmed: call :func:`confirm_operator_capability` before the
    first side effect.
    """
    key = OPERATOR_CAPABILITIES.get(capability)
    if key is None:
        raise ValueError(
            f"unknown operator capability {capability!r}: one of "
            f"{', '.join(sorted(OPERATOR_CAPABILITIES))}"
        )
    authorities = core.claim_authorities
    if not authorities.assigned:
        raise OperatorCapabilityUnavailable(
            f"{command}: the composition root passes no claim_authorities map, "
            f"so nothing owns {capability} (the {key!r} entry); "
            f"{_assignment_fix(key)}"
        )
    path = (
        authorities.declared_issuer
        if key == ISSUER_ENTRY
        else (authorities.verified or {}).get(key)
    )
    if not path:
        raise OperatorCapabilityUnavailable(
            f"{command}: no owner is assigned for {key!r} in the composition "
            f"root's claim_authorities, so nothing here owns {capability}; "
            f"{_assignment_fix(key)}"
        )
    name = authorities.names.get(path)
    if name is None and path in _loaded_paths(core):
        # Loaded here by that path: its name is the loader's.
        name = module_name(path)
    if name is None:
        raise OperatorCapabilityUnavailable(
            f"{command}: owner {key!r} = {path!r} is neither installed nor "
            "loaded in this process, and the composition root declares no "
            "public name for it (its last import segment is not its name): "
            f"write Authority('<name>', module={path!r}) in claim_authorities"
        )
    return OperatorOwner(
        command=command, capability=capability, key=key, module=name, path=path
    )


def _missing_methods(owner: OperatorOwner, missing: Sequence[str]) -> str:
    listed = ", ".join(missing)
    return (
        f"{owner.command}: {owner.label()}; {owner.contract} has no {listed}. "
        "Do it at the external provider, or add the method to "
        f"{owner.module}."
    )


def _grant_fix(core: Any, owner: OperatorOwner, methods: Sequence[str]) -> str:
    caller = _rpc_identity.service_id(core.settings) or "<this service>"
    listed = ", ".join(repr(m) for m in methods)
    return (
        f"grant it in RPC_ALLOWED_CALLS: {{{caller!r}: {{{owner.contract!r}: "
        f"[{listed}, ...]}}}}"
    )


def forbidden_refusal(
    core: Any, owner: OperatorOwner, method: str
) -> OperatorCapabilityUnavailable:
    """The refusal of a 403 answer to ``method``: a missing
    ``RPC_ALLOWED_CALLS`` grant (for a command that catches it on a call)."""
    return OperatorCapabilityUnavailable(
        f"{owner.command}: {owner.label()}; the provider of {owner.contract} "
        f"refuses {method} to this service (403): "
        f"{_grant_fix(core, owner, [method])}"
    )


def _check_shapes(
    owner: OperatorOwner, methods: Sequence[str], schemas: Mapping[str, Any]
) -> None:
    """Refuse (B039) a method whose described schema is not the shape the
    kernel calls it with, before anything is done."""
    problems = [
        f"incompatible {method}: {problem}"
        for method in methods
        if (problem := schema_problem(method, schemas.get(method))) is not None
    ]
    if problems:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; {owner.contract}: "
            + "; ".join(problems)
            + f". Nothing was done: fix the method in {owner.module} (or call "
            "an owner whose contract has this shape)"
        )


def _local_schemas(schema: Any, methods: Sequence[str]) -> Mapping[str, Any] | None:
    """Each method's describe schema from a local contract schema; ``None``
    for a schema that lists names only (a test double: the framework binds
    every registered provider to its :class:`ContractSchema`)."""
    found = schema.methods
    if not isinstance(found, Mapping):
        return None
    out: dict[str, Any] = {}
    for method in methods:
        runtime = found.get(method)
        describe = getattr(runtime, "describe", None)
        out[method] = describe() if callable(describe) else None
    return out


def _declared_refresh(core: Any, owner: OperatorOwner) -> bool | None:
    for descriptor in getattr(core, "module_descriptors", ()) or ():
        if getattr(descriptor, "name", None) == owner.path:
            return bool(getattr(descriptor, "refresh_sessions", True))
    return None


def _confirm_local(
    core: Any, owner: OperatorOwner, methods: Sequence[str]
) -> ConfirmedContract:
    registrant = core.contract_owner(owner.contract)
    if registrant != owner.path:
        raise OperatorOwnerSubstitution(
            f"{owner.command}: {owner.label()}, but {owner.contract} is "
            f"registered here by {registrant or 'the kernel'!r}: only the module "
            "the claim map names answers its operator contract"
        )
    service = core.local_contract(owner.contract)
    have = set(service.contract_schema.methods)
    missing = [m for m in methods if m not in have]
    if missing:
        raise OperatorCapabilityUnavailable(_missing_methods(owner, missing))
    schemas = _local_schemas(service.contract_schema, methods)
    if schemas is not None:
        _check_shapes(owner, methods, schemas)
    return ConfirmedContract(
        contract=service,
        schemas=MappingProxyType(dict(schemas or {})),
        refresh_sessions=_declared_refresh(core, owner),
    )


def _check_binding(core: Any, owner: OperatorOwner) -> str:
    """The pinned ``service_id`` of ``owner``'s bound operator contract."""
    bindings: Mapping[str, Mapping[str, Any]] = core.settings.service_bindings
    binding = bindings.get(owner.contract)
    if binding is None:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; {owner.contract} is neither "
            "registered in this process nor bound: run the command where "
            f"{owner.module} is loaded, or bind {owner.contract} in "
            "SERVICE_BINDINGS (with an RPC_ALLOWED_CALLS grant)"
        )
    pinned = binding.get("service_id")
    if not pinned:
        raise OperatorOwnerSubstitution(
            f"{owner.command}: {owner.label()}; the binding of {owner.contract} "
            "is pinned to no service_id: pin it to the owner's service"
        )
    prefix = f"{owner.module}."
    others = sorted(
        (name, str(other.get("service_id")))
        for name, other in bindings.items()
        if name.startswith(prefix)
        and name != owner.contract
        and other.get("service_id") != pinned
    )
    if others:
        listed = ", ".join(f"{name} -> {sid!r}" for name, sid in others)
        raise OperatorOwnerSubstitution(
            f"{owner.command}: {owner.label()}; the binding of {owner.contract} "
            f"names service {pinned!r}, but the owner's other bindings name "
            f"another ({listed}): bind {owner.contract} to the owner's own "
            "service"
        )
    return str(pinned)


async def _describe(core: Any, owner: OperatorOwner, pinned: str) -> Any:
    try:
        service = core.get_service(owner.contract)
    except ServiceUnavailableInDeployment as exc:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; {owner.contract} is bound but "
            "this process cannot call it (no RPC client is wired)"
        ) from exc
    if not isinstance(service, _proxy.RemoteServiceProxy):
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; {owner.contract} resolves to no "
            "bound provider here"
        )
    try:
        description = await _proxy.describe_provider(service)
    except RpcResponseBindingError as exc:
        raise OperatorOwnerSubstitution(
            f"{owner.command}: {owner.label()}; the describe answer of "
            f"{owner.contract} is not bound to the pinned service {pinned!r}"
        ) from exc
    except Exception as exc:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; {owner.contract} at service "
            f"{pinned!r} cannot be described ({type(exc).__name__}): nothing "
            "was done; retry when it answers"
        ) from exc
    return service, description


async def confirm_operator_capability(
    core: Any, owner: OperatorOwner, methods: Sequence[str]
) -> Any:
    """The contract to call (the local bound provider or the remote proxy):
    :func:`confirm_operator_contract`'s ``contract``."""
    return (await confirm_operator_contract(core, owner, methods)).contract


async def confirm_operator_contract(
    core: Any, owner: OperatorOwner, methods: Sequence[str]
) -> ConfirmedContract:
    """``owner``'s operator contract, confirmed to provide ``methods``.

    In one process: the contract registered here by the module from
    ``owner.path`` (another registrant: :class:`OperatorOwnerSubstitution`),
    its schema listing every method. Else its ``SERVICE_BINDINGS`` entry:
    pinned to the same ``service_id`` as the owner's other bindings, every
    method granted to this service in ``RPC_ALLOWED_CALLS``, and the
    provider's live describe (bound to that service) stating ``owner.path``
    and listing every method. Each method's schema, local or described, is
    compared with the shape the kernel calls it with
    (:data:`OPERATOR_METHOD_SHAPES`): an incompatible one refuses like a
    missing one. Any gap refuses (SANDLOOM-B039 / B040, exit 1) before the
    caller acts.
    """
    if core.service_registry.try_get(owner.contract) is not None:
        return _confirm_local(core, owner, methods)
    if owner.path in _loaded_paths(core):
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()} is loaded here and registers no "
            f"{owner.contract}. Do it at the external provider, or add "
            f"{owner.contract} to {owner.module}."
        )
    pinned = _check_binding(core, owner)
    caller = _rpc_identity.service_id(core.settings) or ""
    ungranted = [
        m
        for m in methods
        if not _rpc_identity.authorize(core.settings, caller, owner.contract, m)
    ]
    if ungranted:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; this service "
            f"({caller or 'no RPC_SERVICE_ID'}) may not call "
            f"{owner.contract}.{', '.join(ungranted)}: "
            f"{_grant_fix(core, owner, ungranted)}"
        )
    service, description = await _describe(core, owner, pinned)
    if description.status == 403:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; service {pinned!r} refuses "
            f"{owner.contract} to this service (403): "
            f"{_grant_fix(core, owner, methods)}"
        )
    if description.status != 200:
        raise OperatorCapabilityUnavailable(
            f"{owner.command}: {owner.label()}; service {pinned!r} does not "
            f"serve {owner.contract} (HTTP {description.status}"
            f"{' ' + description.code if description.code else ''}): register "
            f"it in {owner.module}, or bind it to the service that runs "
            f"{owner.module}"
        )
    if description.provider != owner.path:
        raise OperatorOwnerSubstitution(
            f"{owner.command}: {owner.label()}, but service {pinned!r} answers "
            f"{owner.contract} for {description.provider or 'an unstated module'!r}"
            ": bind it to the service that runs the module the claim map names"
        )
    missing = [m for m in methods if m not in description.methods]
    if missing:
        raise OperatorCapabilityUnavailable(_missing_methods(owner, missing))
    _check_shapes(owner, methods, description.schemas)
    return ConfirmedContract(
        contract=service,
        schemas=MappingProxyType({m: description.schemas.get(m) for m in methods}),
        refresh_sessions=description.refresh_sessions,
    )


async def operator_contract(
    core: Any, capability: str, methods: Sequence[str], *, command: str
) -> tuple[OperatorOwner, Any]:
    """:func:`operator_owner` then :func:`confirm_operator_capability`:
    ``(owner, contract)``, or a refusal before anything is done."""
    owner = operator_owner(core, capability, command=command)
    return owner, await confirm_operator_capability(core, owner, methods)


__all__ = [
    "CODE",
    "OPERATOR_CAPABILITIES",
    "OPERATOR_METHOD_SHAPES",
    "SESSION_KIND_VALUES",
    "SUBSTITUTION_CODE",
    "ConfirmedContract",
    "MethodShape",
    "OperatorCapabilityUnavailable",
    "OperatorOwner",
    "OperatorOwnerSubstitution",
    "answer_problem",
    "confirm_operator_capability",
    "confirm_operator_contract",
    "conforms",
    "forbidden_refusal",
    "operator_contract",
    "operator_owner",
    "render_shape",
    "schema_problem",
]
