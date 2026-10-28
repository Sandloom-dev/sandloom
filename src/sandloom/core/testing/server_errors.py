"""The test client re-raises the exception behind a masked 500 (SANDLOOM-R006).

Production answers an unhandled endpoint error with an opaque
``500 {"code": "internal_error"}`` and logs the cause. In a test that hides
exactly what the developer needs (``MissingTenantError``, a refused event, a
contract registration error): the good message sat in captured logs while the
assertion said ``500 != 201``. The ``client`` / ``real_uow_client`` fixtures
therefore raise :class:`UnhandledServerError` from the original exception —
its type, message and catalogue fix — at the request that produced it.

A test that asserts the 500 itself opts out with
``@pytest.mark.sandloom_allow_server_errors``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sandloom.core import error_codes

#: Marker that keeps the masked 500 (the response a real client sees).
ALLOW_MARKER = "sandloom_allow_server_errors"

#: A message that already names its catalogue entry ("sandloom explain SANDLOOM-R003").
_EXPLAINED = re.compile(r"sandloom explain (SANDLOOM-[CBRT]\d{3})")


class UnhandledServerError(AssertionError):
    """An endpoint raised; the app answered a masked 500 (``__cause__`` = it)."""


def describe(method: str, path: str, exc: BaseException) -> str:
    """The failure text: route, original exception and its catalogue fix."""
    named = _EXPLAINED.search(str(exc))
    code = error_codes.code_for_exception(exc) or (
        named.group(1) if named else "SANDLOOM-R006"
    )
    lines = [
        f"error[{code}]: {method} {path} answered 500 internal_error because the "
        "endpoint raised:",
        f"  {type(exc).__module__}.{type(exc).__qualname__}: {exc}",
        f"  {error_codes.fix_line(code)}",
        "  (production masks this as an opaque 500; mark the test "
        f"@pytest.mark.{ALLOW_MARKER} to assert the 500 itself)",
    ]
    return "\n".join(lines)


@contextmanager
def raise_server_errors(http: Any) -> Iterator[Any]:
    """Make ``http`` (an ``httpx.AsyncClient`` on the app) raise on a masked 500."""
    from sandloom.core.http.error_handlers import observe_unhandled  # noqa: PLC0415 - cold: settings snapshot - the pytest plugin imports this module before .env.test is loaded

    seen: list[tuple[str, str, BaseException]] = []

    async def _on_request(request: Any) -> None:
        seen.clear()

    async def _on_response(response: Any) -> None:
        if response.status_code < 500 or not seen:
            return
        method, path, exc = seen[-1]
        seen.clear()
        raise UnhandledServerError(describe(method, path, exc)) from exc

    hooks = http.event_hooks
    hooks["request"] = [*hooks.get("request", []), _on_request]
    hooks["response"] = [*hooks.get("response", []), _on_response]
    http.event_hooks = hooks
    with observe_unhandled(lambda m, p, e: seen.append((m, p, e))):
        yield http


__all__ = ["ALLOW_MARKER", "UnhandledServerError", "describe", "raise_server_errors"]
