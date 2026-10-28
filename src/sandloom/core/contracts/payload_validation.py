# src/sandloom/core/contracts/payload_validation.py
"""Compiled JSON-Schema validation of event payloads (one interface, two engines).

``compile_payload_validator(schema)`` returns a reusable validator whose
``error(payload)`` is ``None`` for a valid payload or every violation with
its JSON pointer. Backed by **jsonschema-rs** (Rust; install
``sandloom[fast]``)
when importable, else by the pure-Python ``jsonschema`` package — same draft
detection (``$schema``, default 2020-12) and the same format policy (formats
are annotations, not assertions — ``jsonschema``'s default). A schema the Rust
engine cannot compile falls back to ``jsonschema`` (which then reports it
exactly as before, e.g. ``SchemaError``).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from types import ModuleType
from typing import Any, Protocol

from sandloom.core.config.settings import settings

# Both engines are imported on the FIRST compile, not at import: most
# processes (CLI, migrations, workers that never publish a versioned event)
# never validate a payload, and ``jsonschema`` costs tens of ms to import.
_UNSET: Any = object()
_rs_mod: Any = _UNSET
_js_mod: ModuleType | None = None


def _rust() -> Any:
    """``jsonschema_rs`` (sandloom[fast]) or ``None``."""
    global _rs_mod
    if _rs_mod is _UNSET:
        try:
            _rs_mod = importlib.import_module("jsonschema_rs")
        except ImportError:  # pragma: no cover - depends on installed extras
            _rs_mod = None
    return _rs_mod


def _jsonschema() -> ModuleType:
    """The pure-Python ``jsonschema`` package (imported once, lazily)."""
    global _js_mod
    if _js_mod is None:
        import jsonschema  # noqa: PLC0415 - cold: pure-Python jsonschema loads only when a schema is first compiled without jsonschema_rs
        import jsonschema.exceptions  # noqa: PLC0415 - cold: pure-Python jsonschema loads only when a schema is first compiled without jsonschema_rs
        import jsonschema.validators  # noqa: PLC0415 - cold: pure-Python jsonschema loads only when a schema is first compiled without jsonschema_rs

        _js_mod = jsonschema
    return _js_mod


def _force_python() -> bool:
    """``SANDLOOM_JSONSCHEMA_BACKEND=python`` forces the pure-Python engine.

    A ``Settings`` ops field, read when a schema is compiled (never at
    import, spec §2.4).
    """
    return bool(settings.jsonschema_backend == "python")


class PayloadValidator(Protocol):
    """A compiled schema."""

    #: ``"jsonschema-rs"`` or ``"jsonschema"``.
    backend: str

    def error(self, payload: Any) -> str | None:
        """``None`` when ``payload`` is valid, else EVERY violation (``; ``-joined,
        at most :data:`MAX_REPORTED_ERRORS`), each with its JSON pointer."""


class _PythonValidator:
    backend = "jsonschema"

    def __init__(self, schema: dict[str, Any]) -> None:
        # Mirror jsonschema.validate(): pick the draft from $schema, check the
        # schema once, then reuse the validator instance.
        js = _jsonschema()
        self._js = js
        validator_cls = js.validators.validator_for(schema)
        validator_cls.check_schema(schema)
        self._validator = validator_cls(schema)

    def error(self, payload: Any) -> str | None:
        errors = sorted(
            self._validator.iter_errors(payload), key=lambda e: list(e.absolute_path)
        )
        return _join(
            (e.message, "/".join(str(p) for p in e.absolute_path)) for e in errors
        )


class _RustValidator:
    backend = "jsonschema-rs"

    def __init__(self, schema: dict[str, Any]) -> None:
        rs = _rust()
        assert rs is not None  # noqa: S101 - type narrowing of an invariant
        self._schema = schema
        self._validator = rs.validator_for(schema, validate_formats=False)
        self._fallback: _PythonValidator | None = None

    def error(self, payload: Any) -> str | None:
        validator = self._validator
        try:
            if validator.is_valid(payload):
                return None
        except (TypeError, ValueError):
            # A non-JSON value in the payload (e.g. a UUID object): the Python
            # engine judges it exactly as before.
            fallback = self._fallback
            if fallback is None:
                fallback = self._fallback = _PythonValidator(self._schema)
            return fallback.error(payload)
        return (
            _join(
                (str(err.message), "/".join(str(p) for p in err.instance_path))
                for err in validator.iter_errors(payload)
            )
            or "payload does not match the schema"
        )


#: Violations listed per message (the rest are counted).
MAX_REPORTED_ERRORS = 10


def _join(errors: Iterable[tuple[str, str]]) -> str | None:
    """``"msg (at /path); msg2"`` for every violation, or None when valid."""
    shown: list[str] = []
    extra = 0
    for message, path in errors:
        if len(shown) >= MAX_REPORTED_ERRORS:
            extra += 1
            continue
        shown.append(f"{message} (at /{path})" if path else message)
    if not shown:
        return None
    if extra:
        shown.append(f"... and {extra} more")
    return "; ".join(shown)


def payload_validator_backend() -> str:
    """The engine new validators use (``jsonschema-rs`` / ``jsonschema``)."""
    if not _force_python() and _rust() is not None:
        return _RustValidator.backend
    return _PythonValidator.backend


def compile_payload_validator(schema: dict[str, Any]) -> PayloadValidator:
    """Compile ``schema`` once; reuse the result for every payload."""
    if not _force_python() and _rust() is not None:
        try:
            return _RustValidator(schema)
        except Exception:  # noqa: BLE001, S110 - let jsonschema report / handle it
            pass
    return _PythonValidator(schema)


__all__ = [
    "PayloadValidator",
    "compile_payload_validator",
    "payload_validator_backend",
]
