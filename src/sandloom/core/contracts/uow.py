"""Abstract contracts for Unit of Work (technology-agnostic).

Defines the minimal protocol the rest of the system relies on. The UoW
only manages the transaction lifecycle and exposes a transactional context
object via the ``session`` attribute. No repository creation logic belongs
here; business services are responsible for instantiating the data access
patterns they need using the provided session/context.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class UnitOfWorkContract(Protocol):
    """Abstract contract for a Unit of Work.

    Its sole responsibility is managing the transaction.
    """

    session: Any  # Transactional context (e.g. SQLAlchemy AsyncSession)

    async def __aenter__(self) -> UnitOfWorkContract: ...

    async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


@runtime_checkable
class LazyUnitOfWorkContract(UnitOfWorkContract, Protocol):
    """A Unit of Work that can back the lazy request unit (spec §4.3): the
    route runtime's ``db()`` creates it without I/O and the transaction
    begins at the first statement.

    A relational driver hosting request units implements both (the kernel's
    ``LazyUnit`` refuses a database whose UoW does not, naming ``DbSession``
    as the alternative).
    """

    def open_lazily(self) -> Any:
        """Construct the session WITHOUT beginning (no I/O); it begins at its
        first statement. The caller commits / rolls back, then :meth:`close`."""
        ...

    async def close(self) -> None:
        """Close the session and mark the unit exited (every later use of a
        captured reference is refused, SANDLOOM-R002)."""
        ...


__all__ = ["LazyUnitOfWorkContract", "UnitOfWorkContract"]
