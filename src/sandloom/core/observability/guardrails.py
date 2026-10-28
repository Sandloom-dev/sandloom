"""Security & guardrail utilities (Phase K).

Features introduced:
- Redaction helper for structured logging
- Safe task wrapper to isolate failures
- Simple execution time & size guards (extensible)
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar, cast

# pyright: reportUnknownVariableType=false

logger = logging.getLogger("sandloom.core.observability.guardrails")

T = TypeVar("T")

# Matched as case-insensitive SUBSTRINGS of a key name, so compound fields like
# ``db_password``, ``x-api-key``, ``client_secret``, ``stripe_secret_key`` and
# ``set-cookie`` are caught too. Over-redaction is acceptable; under-redaction
# leaks credentials into logs/audit.
SENSITIVE_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "authorization",
    "auth",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "cookie",
}


def _is_sensitive(key: str) -> bool:
    # Normalize separators so ``x-api-key`` matches the ``api_key`` marker.
    low = key.lower().replace("-", "_")
    return any(marker in low for marker in SENSITIVE_KEYS)


def is_sensitive_key(key: str) -> bool:
    """Public: True if a field/attribute name looks like it holds a secret."""
    return _is_sensitive(key)


# Free-text scrubbing (log messages, exception text, tracebacks). Key-based
# redaction cannot see inside an f-string, so these patterns catch the common
# shapes secrets take in prose.
_DSN_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s/@:]*:[^\s/@]+@"
)
_PROVIDER_SECRET = re.compile(
    r"\b(?:sk_(?:live|test)_|rk_(?:live|test)_|whsec_)[A-Za-z0-9_]+"
)
# SQLAlchemy appends "[parameters: (...)]" with the BOUND values to statement
# errors — emails, tokens, password hashes. Drop the whole block.
_SQL_PARAMETERS = re.compile(r"\[parameters: .*?\](?=\s*(?:\n|\(Background|$))", re.S)
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*")
# The key may only START at a word boundary of the ``[\w-]`` run (lookbehind)
# and its prefix is lazy. Without the lookbehind the engine retried the match
# from every position of every word (each retry rescanning the rest of the
# word for a keyword) — quadratic on long identifiers / tracebacks. The
# leftmost match of the old pattern always started at the run start anyway
# (``[\w-]*`` absorbs any prefix) and the greedy suffix runs to the run end in
# both, so the matches — and the output — are identical.
_KEY_VALUE = re.compile(
    r"(?i)(?P<key>(?<![\w\-])[\w\-]*?(?:password|passwd|pwd|secret|token|"
    r"api[_\-]?key|authorization|credential|private[_\-]?key|cookie)[\w\-]*)"
    r"(?P<sep>[\"']?\s*[=:]\s*[\"']?)(?P<value>[^\s,;&\"'\)\]}]+)"
)
# Cheap substring prefilters (ASCII text only: under re.IGNORECASE a few
# non-ASCII letters case-fold onto ASCII — e.g. U+212A KELVIN SIGN matches
# ``k`` — which ``str.lower`` does not reproduce, so non-ASCII text always runs
# every pattern). A pattern whose mandatory literal is absent cannot match.
_KEY_VALUE_MARKERS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api",
    "authorization",
    "credential",
    "private",
    "cookie",
)


def scrub_text(text: str) -> str:
    """Redact secrets embedded in free text (messages, exception strings).

    Masks DSN credentials (``scheme://user:pass@`` -> ``scheme://***@``), SQL
    bound-parameter blocks, bearer tokens and ``<secret-ish key>=<value>``
    pairs. Best-effort and never raises.
    """
    if not text:
        return text
    try:
        out = text
        if "://" in out:
            out = _DSN_CREDENTIALS.sub(r"\g<scheme>***@", out)
        if any(
            prefix in out
            for prefix in ("sk_live_", "sk_test_", "rk_live_", "rk_test_", "whsec_")
        ):
            out = _PROVIDER_SECRET.sub("***", out)
        if "[parameters: " in out:
            out = _SQL_PARAMETERS.sub("[parameters: ***]", out)
        if not out.isascii():
            out = _BEARER.sub(r"\1 ***", out)
            return _KEY_VALUE.sub(r"\g<key>\g<sep>***", out)
        low = out.lower()
        if "bearer" in low:
            out = _BEARER.sub(r"\1 ***", out)
            low = out.lower()
        if any(marker in low for marker in _KEY_VALUE_MARKERS):
            out = _KEY_VALUE.sub(r"\g<key>\g<sep>***", out)
        return out
    except Exception:  # pragma: no cover  # noqa: BLE001 - redaction must never break logging
        return text


def redact(obj: Any) -> Any:
    """Recursively redact sensitive values in common containers.

    Rules: if key (case-insensitive) in SENSITIVE_KEYS -> value replaced with "***".
    Strings longer than 2048 chars truncated for safety.
    Non-collection objects returned as-is.
    """
    try:
        if isinstance(obj, dict):
            obj_dict = cast(dict[str, Any], obj)
            new_dict: dict[str, Any] = {}
            for k_obj, v_obj in obj_dict.items():
                key_str: str = str(k_obj)
                new_dict[key_str] = "***" if _is_sensitive(key_str) else redact(v_obj)
            return new_dict
        if isinstance(obj, list):
            return [redact(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(redact(v) for v in obj)
        if isinstance(obj, set):
            return {redact(v) for v in obj}
        if isinstance(obj, str) and len(obj) > 2048:
            return obj[:2048] + "…"  # truncate long strings
        return obj
    except Exception:  # pragma: no cover  # noqa: BLE001 - redaction must never break logging
        return "<redaction-error>"


class TaskErrorPolicy(Protocol):  # pragma: no cover - structural protocol
    def __call__(self, exc: BaseException) -> None: ...


def log_and_swallow(exc: BaseException) -> None:
    logger.error("Safe task exception: %s", exc, exc_info=True)


def raise_after_log(exc: BaseException) -> None:
    logger.error("Safe task exception (re-raised): %s", exc, exc_info=True)
    raise exc


async def safe_task(
    func: Callable[[], Awaitable[T]],
    *,
    on_error: TaskErrorPolicy = log_and_swallow,
    time_budget: float | None = None,
) -> T | None:
    """Execute async callable isolating errors & optional time guard.

    - Catches all exceptions -> dispatches to `on_error`
    - If time_budget set (seconds) and execution exceeds, logs warning
    - Returns result or None on failure
    """
    start = time.perf_counter()
    try:
        res = await func()
    except Exception as exc:  # noqa: BLE001 - reported through on_error
        on_error(exc)
        return None
    elapsed = time.perf_counter() - start
    if time_budget is not None and elapsed > time_budget:
        logger.warning(
            "Safe task exceeded time budget %.3fs (%.3fs)", time_budget, elapsed
        )
    return res


def enforce_max_size(name: str, value: Any, max_bytes: int) -> Any:
    """Ensure a value's encoded representation doesn't exceed max_bytes.

    Returns original value; raises ValueError if serialized size limit exceeded.
    Currently uses UTF-8 for string-like values and repr() for others.
    """
    if isinstance(value, str | bytes):
        size = len(value.encode("utf-8") if isinstance(value, str) else value)
    else:
        size = len(repr(value).encode("utf-8"))
    if size > max_bytes:
        raise ValueError(f"{name} exceeds max size {max_bytes} bytes (got {size})")
    return value


__all__ = [
    "redact",
    "scrub_text",
    "is_sensitive_key",
    "safe_task",
    "enforce_max_size",
    "log_and_swallow",
    "raise_after_log",
]
