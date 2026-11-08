"""Standard composition root: the kernel app plus every installed module.

The root the framework's image, shipped compose profiles, Celery worker and
beat, ``sandloom migrate`` and the composed operator commands name with
``SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main`` (ADR-0012 decision 3), so
every entry point of a deployment builds the same composition. The kernel's
own ``sandloom.main`` stays the lean root and names no battery.

The composition is the kernel factory with the lean root's default module set
(:func:`sandloom.local_modules.default_modules`): every installed
``sandloom.modules`` entry point (sorted by module name), each required, then
a framework checkout's ``LOCAL_MODULES``, so both roots compose the same
modules; ``SANDLOOM_MODULES`` still chooses an explicit subset. ``pip install
"sandloom[standard]"`` installs identity, tenancy and billing, so
``sandloom_standard.main:app`` serves all three.

The standard trust map (:data:`STANDARD_CLAIM_AUTHORITIES`, ADR-0012 decision
3, ADR-0014): identity issues tokens and is the authority of ``roles``,
``permissions``, ``email`` and ``email_verified``; tenancy of ``tenant_ids``;
billing of ``subscription_status``, ``plan_code``, ``features`` and
``limits``. It is passed as ``default_claim_authorities``, so the kernel keeps
an entry only when its battery is part of the composition (loaded in the
process, or bound in ``SERVICE_BINDINGS`` through its ``<m>.claims.v1``); a
key whose battery is left out has no authority (a definite deny). An
application with its own issuer writes its own map in its own root.
"""

from fastapi import FastAPI

from sandloom.local_modules import default_modules
from sandloom.sdk.app import create_app as _create_kernel_app
from sandloom.sdk.infra import CoreFacade

#: The standard batteries' claim authorities (import paths) and the issuer.
STANDARD_CLAIM_AUTHORITIES: dict[str, str] = {
    "issuer": "sandloom_identity",
    "roles": "sandloom_identity",
    "permissions": "sandloom_identity",
    "email": "sandloom_identity",
    "email_verified": "sandloom_identity",
    "tenant_ids": "sandloom_tenancy",
    "subscription_status": "sandloom_billing",
    "plan_code": "sandloom_billing",
    "features": "sandloom_billing",
    "limits": "sandloom_billing",
}


def standard_modules() -> list[str]:
    """The default set, the lean root's: installed modules, then LOCAL_MODULES."""
    return default_modules()


def create_app() -> FastAPI:
    """Build the standard app: the kernel factory + the installed modules."""
    return _create_kernel_app(
        default_modules=standard_modules(),
        default_claim_authorities=STANDARD_CLAIM_AUTHORITIES,
    )


app = create_app()
core: CoreFacade = app.state.core

__all__ = [
    "STANDARD_CLAIM_AUTHORITIES",
    "app",
    "core",
    "create_app",
    "standard_modules",
]
