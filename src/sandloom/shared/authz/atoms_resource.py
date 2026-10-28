"""Tier-2 atoms: checks over the request resource (no extra I/O).

The resource is whatever entity the endpoint operates on (a post, an invoice…).
It is loaded **once**, lazily, by the dependency that runs the policy, and the
same loaded object is handed to the endpoint — never fetched twice. ``Owner``
reads ownership through a module-supplied extractor, so this layer stays free of
any ORM import (``shared`` never imports a business module's models).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sandloom.shared.authz.algebra import TIER_RESOURCE, Requirement

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.shared.authz.context import AuthzContext


class Owner(Requirement):
    """Subject owns the request resource.

    ``get_owner`` maps the loaded resource to its owner id; it is supplied by
    the module declaring the policy (e.g. ``lambda post: post.author_id``), so
    the atom never needs to know the resource's type.
    """

    tier = TIER_RESOURCE

    def __init__(self, get_owner: Callable[[Any], UUID | None]) -> None:
        self.get_owner = get_owner

    async def evaluate(self, ctx: AuthzContext) -> bool:
        resource = await ctx.get_resource()
        if resource is None:
            return False
        owner_id = self.get_owner(resource)
        return owner_id is not None and owner_id == ctx.subject.user_id

    def describe(self) -> str:
        return "resource owner"
