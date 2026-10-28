"""The typed contract reference and the generated DTO copies: pure data
types with a kernel-level home.

The generated contract stubs in ``<shared>.contracts.*`` declare
``X_Y_V1: ContractRef[XYV1] = ContractRef("x.y.v1", XYV1)``, and the generated
DTO modules (``<shared>.contracts.<module>_dto``) mark each wire-shape copy of
a provider's DTO with :func:`dto_copy`. The kernel (``sandloom.core``,
``sandloom.shared``) never imports ``sandloom.sdk`` (import-linter contract
``kernel-never-imports-sdk``), so both live here and ``sandloom.sdk.contracts``
re-exports :class:`ContractRef` (one class, so ``isinstance`` holds on both
spellings).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Generic, TypeVar

P = TypeVar("P")
T = TypeVar("T", bound=type)

#: The attribute :func:`dto_copy` records the provider class in.
DTO_COPY_ATTR = "__sandloom_dto__"


class ContractRef(Generic[P]):
    """A typed reference to a contract (emitted by the stub generator):
    ``STATS_QUOTA_V1: ContractRef[StatsQuotaV1] = ContractRef("stats.quota.v1",
    StatsQuotaV1)``.

    ``protocol`` is the stub's Protocol: a ``Contract(REF)`` caller receives
    results as its methods' annotations name them (the generated DTO copies),
    locally and over RPC. ``None`` (a hand-written reference): results as the
    runtime returns them."""

    __slots__ = ("name", "protocol")

    def __init__(self, name: str, protocol: type[P] | None = None) -> None:
        self.name = name
        self.protocol = protocol

    def __repr__(self) -> str:
        return f"ContractRef({self.name!r})"


_COPIES: dict[str, list[type]] = {}
_LOCK = threading.Lock()


def dto_copy(origin: str) -> Callable[[T], T]:
    """Mark a generated wire-shape copy of the provider DTO ``origin``
    (``"<module>:<qualname>"``) and register it, so an RPC caller that does
    not import the provider decodes the provider's class into a copy
    (:func:`dto_copies`). Written by ``sandloom generate``; never by hand."""

    def mark(cls: T) -> T:
        setattr(cls, DTO_COPY_ATTR, origin)
        with _LOCK:
            known = _COPIES.setdefault(origin, [])
            if cls not in known:
                known.append(cls)
        return cls

    return mark


def dto_copies(origin: str) -> tuple[type, ...]:
    """The loaded generated copies of the provider DTO ``origin``, in the
    order their modules were imported."""
    return tuple(_COPIES.get(origin, ()))


def copy_origin(cls: Any) -> str | None:
    """The provider DTO a generated copy was made from, else ``None``."""
    origin = getattr(cls, "__dict__", {}).get(DTO_COPY_ATTR)
    return origin if isinstance(origin, str) else None


__all__ = ["DTO_COPY_ATTR", "ContractRef", "copy_origin", "dto_copies", "dto_copy"]
