"""An instance's options as a process that boots no application reads them.

``celery -A sandloom_celery.worker inspect`` and Flower start a process with no
composition root and no application: no registry is built, nothing is opened.
Such a process still needs the broker of the ``default`` task queue, and the
broker is an option of that instance (``INFRA.taskqueue.default.broker``). A
driver package imports only ``sandloom.driverkit`` (ADR-0016), so it cannot read
the kernel's settings itself; this module reads them for it, once, the same way
the application does: ``Settings`` (environment, ``.env``), the composition's
selection, ``INFRA`` over it and the options each installed driver takes from a
flat setting that still exists.

It opens no connection and builds no provider. An invalid configuration stops
the process with the configuration error, as at every entry point
(:func:`sandloom.core.config.settings.load_settings`).
"""

from __future__ import annotations

from sandloom.core.config.settings import load_settings
from sandloom.core.infra.config import DEFAULT_INSTANCE, InstanceConfig
from sandloom.core.infra.tool_infra import filled_infra_config

__all__ = ["process_instance"]


def process_instance(
    capability: str, name: str = DEFAULT_INSTANCE
) -> InstanceConfig | None:
    """The configured instance ``capability.name``, or ``None`` when the
    configuration declares none.

    ``driver`` says which driver the application would use for it, so a caller
    checks that the instance is its own before it reads an option. The options
    are a copy: changing them changes nothing. A driver of the configuration
    that cannot be loaded raises ``RuntimeError`` with the reason.
    """
    filled = filled_infra_config(load_settings())
    if filled.unfilled is not None:
        # A driver of the configuration cannot be loaded: its options would
        # come back without what it takes from a flat setting.
        raise RuntimeError(
            f"cannot read the configuration of {capability}.{name}: {filled.unfilled}"
        )
    found = filled.config.get(capability, {}).get(name)
    if found is None:
        return None
    return InstanceConfig(
        capability=found.capability,
        name=found.name,
        driver=found.driver,
        options=dict(found.options),
        implicit=found.implicit,
    )
