"""contract_method keeps assigned annotations on every Python version.

Python 3.14's functools.wraps copies ``__annotate__`` rather than
``__annotations__`` (PEP 649), so a callable whose annotations were assigned
at runtime (generated fakes, decorators) lost them behind the wrapper and
the wire-type check reported "missing type annotation".
"""

import typing

from sandloom.core.module_api import contract_method


def test_assigned_annotations_survive_the_wrapper() -> None:
    async def method(cls, amount, operation_key):  # type: ignore[no-untyped-def]
        return None

    method.__annotations__ = {"amount": int, "operation_key": str, "return": int}
    wrapped = contract_method(command=True, idempotency_key="operation_key")(method)

    assert typing.get_type_hints(wrapped) == {
        "amount": int,
        "operation_key": str,
        "return": int,
    }
