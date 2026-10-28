"""Tenancy API routers."""

from .invitations import invitations_router
from .organizations import organizations_router
from .platform import platform_organizations_router

__all__ = [
    "platform_organizations_router",
    "organizations_router",
    "invitations_router",
]
