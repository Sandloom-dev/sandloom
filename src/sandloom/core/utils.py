import inspect
import json
import uuid
from collections.abc import Callable
from typing import Any


def generate_id() -> str:
    return str(uuid.uuid4())


def safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def takes(fn: Callable[..., Any], count: int, *, follow_wrapped: bool = True) -> bool:
    """Whether ``fn`` takes ``count`` positional arguments: it requires that
    many, or accepts any number (``*args``).

    How an API that takes a callable in two shapes (with or without a leading
    session, the session being ``db()``) tells which one it was given. Only a
    required parameter counts: ``load(id, include_archived=False)`` takes one
    argument, so its second parameter is never handed the session. A function
    that fits both shapes (``*args``) fits any count.
    ``follow_wrapped=False`` reads the signature of a wrapper itself, not of
    what it wraps.
    """
    signature = inspect.signature(fn, follow_wrapped=follow_wrapped)
    try:
        signature.bind(*([None] * count))
    except TypeError:
        return False
    parameters = signature.parameters.values()
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters):
        return True
    required = sum(
        1
        for p in parameters
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and p.default is inspect.Parameter.empty
    )
    return required == count
