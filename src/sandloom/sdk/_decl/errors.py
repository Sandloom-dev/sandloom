"""The refusals of the declarative level (L1), each with its catalogue code.

Every exception carries ``sandloom_code`` (``sandloom explain <code>`` prints the long
form and the fix). They are raised at class creation, at decoration or by
discovery (``define_module``), so a wrong declaration fails the boot before
any route serves (spec Appendix A).
"""

from __future__ import annotations


class DeclarationError(TypeError):
    """Base class of the L1 declaration refusals."""

    sandloom_code = "SANDLOOM-B006"

    def __init__(self, message: str, *, fix: str | None = None) -> None:
        text = f"[{self.sandloom_code}] {message}"
        if fix:
            text += f"\n  fix: {fix}"
        text += f"\n  more: sandloom explain {self.sandloom_code}"
        super().__init__(text)


class ModuleDiscoveryError(DeclarationError):
    """Discovery refused the module: a declaration in the wrong place, two
    declarations claiming one name (SANDLOOM-B006)."""

    sandloom_code = "SANDLOOM-B006"


class EventDefinitionError(DeclarationError):
    """An invalid ``Event`` class, or ``.publish()`` on a view (SANDLOOM-B015;
    a view's ``.publish()`` is SANDLOOM-C025 statically)."""

    sandloom_code = "SANDLOOM-B015"


class EventMetaUnavailable(AttributeError):
    """``event.meta`` read on an event that was not delivered."""

    sandloom_code = "SANDLOOM-B015"


class ConsumerDefinitionError(DeclarationError):
    """An invalid ``@on`` consumer (SANDLOOM-B016)."""

    sandloom_code = "SANDLOOM-B016"


class ContractDefinitionError(DeclarationError):
    """An invalid ``@contract`` class or ``@command`` method (SANDLOOM-B018)."""

    sandloom_code = "SANDLOOM-B018"


class ContractDtoError(ContractDefinitionError):
    """A ``@contract`` signature names a DTO outside the closed set of wire
    shapes, or one ``sandloom generate`` cannot copy (SANDLOOM-C012)."""

    sandloom_code = "SANDLOOM-C012"


class ProjectionDefinitionError(DeclarationError):
    """An invalid ``Projection`` (SANDLOOM-B012)."""

    sandloom_code = "SANDLOOM-B012"


__all__ = [
    "ConsumerDefinitionError",
    "ContractDefinitionError",
    "ContractDtoError",
    "DeclarationError",
    "EventDefinitionError",
    "EventMetaUnavailable",
    "ModuleDiscoveryError",
    "ProjectionDefinitionError",
]
