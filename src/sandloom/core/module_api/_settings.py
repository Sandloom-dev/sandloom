"""Module settings: where the validated instances live and how they are read
(ADR-0015 decision 1).

The configuration half (snapshot, validation, the post-load check) is
:mod:`sandloom.core.config.module_settings`. Here:

- :func:`declare`: ``api.declare_settings(Model)`` / ``define_module(settings=)``
  validates the model before ``setup`` and buffers the record in the
  registration draft (applied with the rest of the module, or not at all);
- :func:`settings_of`: a module's reader, from inside a request, a task or
  its ``setup``; with no application bound it raises, so a read at import
  fails the import audit;
- :func:`override_settings` and :func:`validate_settings`: the testing seams
  (public through ``sandloom.sdk.testing``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

import sandloom.core.config.module_settings as _ms
import sandloom.core.config.settings as _kernel
from sandloom.core.config.module_settings import (
    ModuleSettingsError,
    RemovedSetting,
    Risk,
    SemanticSetting,
)
from sandloom.core.descriptors.module_names import module_name as _module_name
from sandloom.core.runtime import get_active_core

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.module_api._registration import ModuleRegistrationDraft

M = TypeVar("M", bound=BaseModel)


class SettingsNotBoundError(RuntimeError):
    """``settings_of`` found no instance: no application bound (a read at
    import time), or no loaded module declared the model."""


def declare(
    facade: Any,
    draft: ModuleRegistrationDraft | None,
    model: type[M],
    package: str,
) -> M:
    """Validate ``model`` for ``package`` from the process snapshot (before
    ``setup``) and buffer it in ``draft`` (applied on the module's commit);
    without a draft (a bare ``ModuleAPI`` in a test) it applies at once."""
    _ms.check_declaration(model)
    record = _ms.validate_module_settings(
        model,
        package=package,
        module=_module_name(package),
        kernel=facade.settings,
    )
    if draft is not None:
        draft.module_settings.append(record)
    else:
        registry_of(facade).add(record)
    return record.instance  # type: ignore[return-value]


def registry_of(facade: Any) -> _ms.ModuleSettingsRegistry:
    """The module settings of the application ``facade`` belongs to."""
    registry = getattr(facade, "module_settings", None)
    if isinstance(registry, _ms.ModuleSettingsRegistry):
        return registry
    raise SettingsNotBoundError(f"{facade!r} carries no module settings")


#: Testing overrides (``override_settings``): model -> instance.
_OVERRIDES: dict[type[BaseModel], BaseModel] = {}


def settings_of(model: type[M]) -> M:
    """The validated settings ``model`` of the running application.

    Call it where the application is bound (a request, a task, a consumer,
    the module's ``setup``), never at import: with no application bound it
    raises :class:`SettingsNotBoundError`, which is how the import audit
    catches a module-level read.
    """
    if _OVERRIDES:
        overridden = _OVERRIDES.get(model)
        if overridden is not None:
            return overridden  # type: ignore[return-value]
    registering = _ms.REGISTERING.get()
    if registering:
        for record in registering:
            if record.model is model:
                return record.instance  # type: ignore[return-value]
    core = get_active_core()
    if core is None:
        raise SettingsNotBoundError(
            f"settings_of({model.__name__}) with no application bound: read "
            "settings inside a request, a task or setup, never at import"
        )
    registry = getattr(core, "module_settings", None)
    found: M | None = registry.get(model) if registry is not None else None
    if found is None:
        raise SettingsNotBoundError(
            f"settings_of({model.__name__}): no module loaded in this "
            f"application declared {model.__name__} "
            "(define_module(..., settings=...))"
        )
    return found


@contextmanager
def override_settings(model: type[M], **values: Any) -> Iterator[M]:
    """Testing: ``settings_of(model)`` returns a copy with ``values`` (field
    names) inside the block; the application's instance is untouched.

    Starts from the bound application's instance, else from the model's
    defaults. Usable in a fixture (``with override_settings(...): yield``)
    and nestable; process-wide, so threads and tasks see it too.
    """
    unknown = sorted(set(values) - set(model.model_fields))
    if unknown:
        raise TypeError(
            f"{model.__name__} has no field(s) {unknown}; pass field names "
            f"(fields: {sorted(model.model_fields)})"
        )
    try:
        base = settings_of(model)
    except SettingsNotBoundError:
        base = model.model_construct()
    instance = base.model_copy(update=values)
    missing = object()
    previous = _OVERRIDES.get(model, missing)
    _OVERRIDES[model] = instance
    try:
        yield instance
    finally:
        if previous is missing:
            _OVERRIDES.pop(model, None)
        else:
            _OVERRIDES[model] = previous  # type: ignore[assignment]


def validate_settings(
    model: type[M],
    env: Mapping[str, str] | None = None,
    *,
    environment: str | None = None,
    accepted_risks: Iterable[str] | None = None,
    package: str | None = None,
) -> M:
    """Testing: validate ``model`` exactly as the loader does before ``setup``.

    ``env`` is the whole environment to read (no ``.env``, no secrets);
    omitted, the process snapshot is read. ``environment`` (``"prod"``...) and
    ``accepted_risks`` (``<module>.<name>`` ids) replace the kernel's in the
    validation context. Raises :class:`ModuleSettingsError` with the message
    a boot would print.
    """
    pkg = package or model.__module__.rsplit(".", 1)[0]
    core = get_active_core()
    kernel = core.settings if core is not None else _kernel.settings
    snapshot = (
        _ms.SourceSnapshot(environ=env, env_file=None) if env is not None else None
    )
    record = _ms.validate_module_settings(
        model,
        package=pkg,
        module=_module_name(pkg),
        kernel=kernel,
        snapshot=snapshot,
        environment=environment,
        accepted_risks=accepted_risks,
    )
    return record.instance  # type: ignore[return-value]


__all__ = [
    "ModuleSettingsError",
    "RemovedSetting",
    "Risk",
    "SemanticSetting",
    "SettingsNotBoundError",
    "declare",
    "override_settings",
    "registry_of",
    "settings_of",
    "validate_settings",
]
