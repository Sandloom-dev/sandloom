"""Contract method specs and session-parameter helpers (no runtime state)."""

from __future__ import annotations

import functools
import inspect
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import sandloom.core._internal.db_provider as _db_provider

#: Attribute a :func:`contract_method` stamps on the wrapped function.
CONTRACT_SPEC_ATTR = "__sandloom_contract__"
#: Attribute :func:`undigested` stamps on a command's function: the
#: parameters left out of its stored args digest.
UNDIGESTED_ATTR = "__sandloom_undigested__"
#: Parameter names treated as an (injected / caller-passed) database session.
SESSION_PARAM_NAMES = frozenset({"db", "session"})


@dataclass(frozen=True, slots=True)
class ContractMethodSpec:
    """How the contract runtime runs one contract method."""

    kind: Literal["read", "command"] = "read"
    #: Name of the argument carrying the caller's idempotency/operation key
    #: (mandatory for commands).
    idempotency_key: str | None = None
    #: Database for DIRECT (non-registry) calls; registry-resolved calls use
    #: the provider module's declared database.
    database: str | None = None
    #: A read contract made ONLY of declared planner operations
    #: (``contract_reads()``): no Unit of Work is opened; the body runs in a
    #: read-only scope whose reads lease from the contract pool (general or
    #: optimized executor, see ``sandloom.core.planner``).
    planned: bool = False
    #: Selected domain arguments partition operation keys within a tenant.
    idempotency_scope: tuple[str, ...] = ()
    #: The risk of a command's effect, declared at its source (ADR-0009
    #: item 7: ``risk="money"``); ``None`` for none.
    risk: str | None = None

    @property
    def readonly(self) -> bool:
        return self.kind == "read"


def undigested(*names: str) -> Callable[[Any], Any]:
    """Keep the secret arguments ``names`` of a command out of every stored
    record: its args digest (the durable command log and the cache
    reservation) is taken over the other arguments only.

    For a single-use secret the caller never repeats under one key (a reset
    token, a password set through it). The same key with another value of an
    undigested argument therefore REPLAYS instead of being refused as a
    conflict: use a fresh key per attempt. Stack it on the function with
    ``@command`` (either order); the idempotency key and the
    ``idempotency_scope`` arguments cannot be undigested.
    """
    if not names or any(not isinstance(n, str) or not n for n in names):
        raise ValueError("undigested() needs the names of the secret parameters")
    if len(set(names)) != len(names):
        raise ValueError("undigested() names a parameter twice")

    def mark(func: Any) -> Any:
        setattr(func, UNDIGESTED_ATTR, tuple(names))
        return func

    return mark


def undigested_names(func: Any) -> tuple[str, ...]:
    """The secret parameters :func:`undigested` marked on ``func``."""
    return tuple(getattr(func, UNDIGESTED_ATTR, ()) or ())


def digested_arguments(func: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """``arguments`` without ``func``'s undigested (secret) ones."""
    secret = undigested_names(func)
    if not secret:
        return arguments
    return {k: v for k, v in arguments.items() if k not in secret}


def contract_spec(method: Any) -> ContractMethodSpec | None:
    """The :class:`ContractMethodSpec` of a (bound) contract method, if any."""
    spec = getattr(method, CONTRACT_SPEC_ATTR, None)
    return spec if isinstance(spec, ContractMethodSpec) else None


# Hot-path caches: inspect.signature / typing.get_type_hints re-parse the
# function on every call (tens of microseconds each) — compute them once.
@functools.lru_cache(maxsize=4096)
def _signature_of(func: Callable[..., Any]) -> inspect.Signature:
    return inspect.signature(func)


@functools.lru_cache(maxsize=4096)
def _return_annotation_of(func: Callable[..., Any]) -> Any:
    try:
        return typing.get_type_hints(func).get("return", Any)
    except Exception:  # noqa: BLE001 - unresolved hints: untyped decode
        return Any


def session_parameters(method: Callable[..., Any]) -> list[str]:
    """Names of ``method``'s parameters that are a database session.

    Matched by name (``db`` / ``session``) or by the relational session type
    annotation — the shapes a contract method must no longer declare.
    """

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return []
    try:
        hints = typing.get_type_hints(method)
    except Exception:  # noqa: BLE001 - unresolved hints: fall back to names
        hints = {}
    names: list[str] = []
    for name, param in signature.parameters.items():
        annotation = hints.get(name, param.annotation)
        if name in SESSION_PARAM_NAMES or _db_provider.is_relational_session_type(
            annotation
        ):
            names.append(name)
    return names


def contract_kind(method: Any) -> Literal["read", "command"] | None:
    """How a BOUND contract member runs: ``read`` / ``command``, or ``None``
    for a plain (non-contract) member of the provider."""
    spec = contract_spec(method)
    return spec.kind if spec is not None else None
