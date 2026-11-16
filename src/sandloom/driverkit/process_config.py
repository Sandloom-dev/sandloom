"""An instance's options as a process that boots no application reads them.

``celery -A sandloom_celery.worker inspect`` and Flower start a process with no
composition root and no application: no registry of the application is built,
nothing is opened. Such a process still needs the broker of the ``default`` task
queue, and the broker is an option of that instance
(``INFRA.taskqueue.default.broker``). A driver package imports only
``sandloom.driverkit`` (ADR-0016), so it cannot read the kernel's settings
itself; this module reads them for it, the way ``sandloom worker check`` does:
``Settings`` (environment, ``.env``), the composition's selection and ``INFRA``
over it, for the capabilities such a process configures (``taskqueue``) and no
other. No other capability's driver is imported: a broken or slow driver of the
database or the event bus cannot break a ``celery inspect``.

This is the door of a process that boots no application. It answers with the
options of the instance as configured, a stated secret included, so it is not for
a process that serves requests, and it is not a general reader of ``INFRA``.

It opens no connection and builds no provider. An invalid configuration stops
the process with the configuration error, as at every entry point
(:func:`sandloom.core.config.settings.load_settings`); a driver of the
configured task queue that cannot be loaded raises the registry's error with its
reason.
"""

from __future__ import annotations

from sandloom.core.config.settings import load_settings
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
from sandloom.core.infra.tool_infra import build_tool_infra

__all__ = ["process_instance"]

#: The capabilities a process that boots no application configures: the ones
#: ``sandloom.core.infra.tool_infra.build_tool_infra`` builds (a test keeps the
#: two equal).
_PROCESS_CAPABILITIES = frozenset({"taskqueue"})


def process_instance(
    capability: str, name: str = DEFAULT_INSTANCE
) -> InstanceConfig | None:
    """The configured instance ``capability.name``, or ``None`` when the
    configuration declares none.

    ``capability`` is ``taskqueue``; any other raises ``ValueError`` (such a
    process does not configure it, so ``None`` would mislead). ``driver`` says
    which driver the application would use for it, so a caller checks that the
    instance is its own before it reads an option. The options are a copy:
    changing them changes nothing.
    """
    if capability not in _PROCESS_CAPABILITIES:
        raise ValueError(
            "a process that boots no application configures only "
            f"{sorted(_PROCESS_CAPABILITIES)}, not {capability!r}"
        )
    load_settings()
    found = build_tool_infra().instance_config(capability, name)
    if found is None:
        return None
    return InstanceConfig(
        capability=found.capability,
        name=found.name,
        driver=found.driver,
        options=dict(found.options),
        implicit=found.implicit,
    )
