"""The url of a Redis instance: an option of that instance, never inherited.

Every url and client bound of a Redis instance is an option of that instance
(``INFRA``): no ``CACHE_URL`` / ``CACHE_*`` setting fills one, and no instance
takes an option from another. An instance that has no url refuses startup
naming its own INFRA option (``cache.<instance>.url``, see :func:`require_url`).
The Redis drivers take nothing from the flat settings.
"""

from __future__ import annotations

import json


class MissingUrlError(ValueError):
    """A ``redis`` instance has no ``url``: a configuration mistake that aborts
    startup in every environment (``fatal_at_startup``), never a transient fault.

    Fatal everywhere because a non-strict (development) startup only logs any
    other failure and goes on, and the auth revocation and block checks then
    read the unreachable store as "not revoked": a missing url must never
    reach that path.
    """

    fatal_at_startup = True


def require_url(
    capability: str,
    instance: str,
    url: str | None,
    *,
    fallback: str | None = None,
) -> str:
    """The ``url``, or refuse startup (:class:`MissingUrlError`) when it is empty.

    The message names the INFRA option to set (``<capability>.<instance>.url``)
    with an example. ``fallback`` says what else the instance would have used
    and found empty too (``None``: it has no fallback). Every Redis driver calls
    this from its ``aopen``, so ``InfraRegistry.aopen_all`` reports the mistake
    before the first connection.
    """
    if url:
        return url
    example = {capability: {instance: {"url": "redis://localhost:6379/0"}}}
    tail = f" ({fallback} has none either)" if fallback else ""
    raise MissingUrlError(
        f"redis {capability} instance {instance!r} has no url{tail}: set the "
        f"INFRA option {capability}.{instance}.url, e.g. "
        f"INFRA='{json.dumps(example)}'"
    )
