"""Identity API module.

``define_module`` mounts every router named in ``__all__``, in this order.
"""

from .auth import auth_router
from .roles import roles_router
from .sso import sso_router
from .twofa import twofa_router
from .user_roles import user_roles_router
from .user_safety import user_safety_router
from .users import users_router

__all__ = [
    "auth_router",
    "roles_router",
    "sso_router",
    "twofa_router",
    "user_roles_router",
    "user_safety_router",
    "users_router",
]
