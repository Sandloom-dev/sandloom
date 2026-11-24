"""The refusals of the declarative level (L1), each with its catalogue code.

Every exception carries ``sandloom_code`` (``sandloom explain <code>`` prints the long
form and the fix). They are raised at class creation, at decoration or by
discovery (``define_module``), so a wrong declaration fails the boot before
any route serves (spec Appendix A).
"""

from __future__ import annotations

from fastapi import HTTPException


class Refused(HTTPException):
    """A request the module refuses on purpose: ``raise Refused(402, "item quota
    reached")``.

    The one error type a service, a route, a contract method or a saga local
    step raises for a client-side refusal (status 400-499; a server fault is any
    other exception, which the framework masks as a 500). It is an
    ``HTTPException``, so a route answers ``{"detail": ..., "code": ...}`` with
    that status and rolls the unit back; a contract caller receives it as a
    ``ContractCallError`` with the same status; a saga local step is refused
    for good on 400, 402, 403, 404, 410 and 422 (any other status is retried
    until the saga's deadline).

    ``detail`` defaults to the status phrase ("Payment Required"); ``code`` is a
    stable machine-readable name a client can branch on; ``headers`` go out with
    the response.
    """

    code: str | None = None

    def __init__(
        self,
        status: int,
        detail: str | None = None,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if isinstance(status, bool) or not isinstance(status, int):
            raise TypeError(f"Refused takes an integer status, not {status!r}")
        if not 400 <= status <= 499:
            raise ValueError(
                f"Refused takes a client status 400-499, not {status}: a server "
                "fault is a plain exception, which the framework answers as a 500"
            )
        super().__init__(status_code=status, detail=detail, headers=headers)
        if code is not None:
            self.code = code


class NotFound(Refused):
    """The row or resource the request names does not exist (404):
    ``raise NotFound("item not found")``. Also what a row of another tenant is:
    never reveal that it exists.

    Not ``sandloom.sdk.infra.NotFound``, the error an object store raises for a
    missing key: an unrelated class (it is no ``Refused``), so ``except
    NotFound`` with this one does not catch a storage miss, and one left
    uncaught is a 500. Catch the storage one and raise this one::

        from sandloom.sdk.infra import NotFound as ObjectMissing

        try:
            data = await objectstore().get(key)
        except ObjectMissing:
            raise NotFound("report not found") from None
    """

    code = "not_found"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(404, detail, code=code, headers=headers)


class Conflict(Refused):
    """The request clashes with the current state (409): a duplicate slug, a
    stale version. ``raise Conflict("slug already taken")``. A saga local step
    retries a 409 until its deadline; a step that must refuse for good raises
    ``Refused(422, ...)``."""

    code = "conflict"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(409, detail, code=code, headers=headers)


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
    "Conflict",
    "ConsumerDefinitionError",
    "ContractDefinitionError",
    "ContractDtoError",
    "DeclarationError",
    "EventDefinitionError",
    "EventMetaUnavailable",
    "ModuleDiscoveryError",
    "NotFound",
    "ProjectionDefinitionError",
    "Refused",
]
