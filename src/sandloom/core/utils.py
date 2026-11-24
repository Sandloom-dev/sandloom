import inspect
import json
import uuid
from collections.abc import Callable
from typing import Any


def generate_id() -> str:
    return str(uuid.uuid4())


def safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def takes_one_argument(fn: Callable[..., Any]) -> bool:
    """Whether ``fn`` can be called with one positional argument.

    How an API that hands its callable one value (a loader gets the id, a
    projection handler the envelope) refuses the earlier form that also took
    the session first: ``load(id, session)`` needs two arguments, so it is
    refused where it is declared, with the rewrite in the message. A defaulted
    option (``load(id, include_archived=False)``) keeps its default.

    Both signatures are read, the one a decorator shows (``__wrapped__``
    followed) and the function's own: a decorated ``load(id, session)`` is
    refused, and so is a framework adapter that needs the session first.
    """
    for follow_wrapped in (True, False):
        try:
            inspect.signature(fn, follow_wrapped=follow_wrapped).bind(None)
        except TypeError:
            return False
    return True
