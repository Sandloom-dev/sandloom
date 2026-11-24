import inspect
import json
import uuid
from collections.abc import Callable
from typing import Any


def generate_id() -> str:
    return str(uuid.uuid4())


def safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def binds(fn: Callable[..., Any], count: int, *, follow_wrapped: bool = True) -> bool:
    """Whether ``fn`` can be called with ``count`` positional arguments.

    How an API that takes a callable in two shapes (with or without a leading
    session, the session being ``db()``) tells which one it was given. A
    function that fits both (``*args``) fits any count. ``follow_wrapped=False``
    reads the signature of a wrapper itself, not of what it wraps.
    """
    try:
        inspect.signature(fn, follow_wrapped=follow_wrapped).bind(*([None] * count))
    except TypeError:
        return False
    return True
