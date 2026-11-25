"""The process behind ``sandloom dev token`` (:mod:`sandloom.core.cli.dev.token`).

Run as ``python -m sandloom.core.cli.dev.token_child '<request json>'`` in the
app's checkout, as the composition root is read in ``sandloom dev`` elsewhere:
the app's package is the checkout's and nothing of it, settings included, is
imported into the CLI. In order:

1. load the app's settings and apply the dev-only rule to them again (the
   parent checked the raw ``ENVIRONMENT`` / ``DATABASE_URL`` and the libpq
   ``PGHOST`` / ``PGHOSTADDR`` / ``PGSERVICE``; this is the value the app
   would run with, ``SECRETS_DIR`` included);
2. import the composition root, which composes the modules and so assigns the
   claim authorities;
3. mint (:func:`sandloom.core.auth.dev_token.mint_dev_token`, which checks the
   environment a third time) and answer with one sentinel line of JSON:
   ``{"token", "summary", "notes"}`` or ``{"refused": why}``.

A failure it does not name exits non-zero with a traceback on stderr, which
the parent hides unless ``--debug`` (a settings error echoes its input, and the
secret is one). The JWT secret and key are never read here, only used by the
token signer, and no answer carries them.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from sandloom.core.auth.dev_token import (
    DEFAULT_TTL_MINUTES,
    DevToken,
    DevTokenRefused,
    mint_dev_token,
)
from sandloom.core.cli.dev import errors as _errors
from sandloom.core.cli.dev import token as _token
from sandloom.core.cli.url_text import drop_urls
from sandloom.core.composition import CompositionRootError, load_composition_core
from sandloom.core.config.settings import load_settings


def _emit(answer: dict[str, object]) -> int:
    # The leading newline ends a line the app left open (a banner printed
    # without one): the answer always starts its own line.
    print("\n" + _token.SENTINEL + json.dumps(answer), flush=True)
    return 0


def _refuse(why: str) -> int:
    return _emit({"refused": why})


def _summary(minted: DevToken) -> str:
    return (
        f"dev token for user {minted.user_id}"
        f", tenant {minted.tenant_id or 'none'}"
        f", roles {', '.join(minted.roles) or 'none'}"
        f", valid {minted.ttl_minutes} minutes"
        f", vouched by {', '.join(minted.authorities) or 'no authority'}"
    )


def _notes(minted: DevToken, *, tenancy_mode: str, tenant_authority: bool) -> list[str]:
    """What the developer is about to run into with this token."""
    if minted.tenant_id is not None:
        return []
    if tenancy_mode == "single":
        # The configured tenant binds for every token; `--tenant` would only
        # have to match it (403 tenant_mismatch otherwise), a module granting
        # tenants or not.
        return []
    if tenant_authority:
        return [
            "no --tenant: a tenant-scoped route answers 400 tenant_required; "
            "pass --tenant <uuid> for the tenant to act in"
        ]
    return [
        "TENANCY_MODE=multi and no composed module grants a tenant: a "
        "tenant-scoped route cannot bind one for any token. For a first try set "
        "TENANCY_MODE=single and SINGLE_TENANT_ID=<uuid> in .env, or compose the "
        "tenancy module"
    ]


def main(argv: Sequence[str]) -> int:
    # ``python -m`` puts the cwd first on ``sys.path`` unless PYTHONSAFEPATH=1
    # drops it: the checkout's own package would not be found then.
    sys.path.insert(0, os.getcwd())
    request = json.loads(argv[0])
    try:
        loaded = load_settings()
    except SystemExit:
        # Its text echoes the input that failed validation, a secret included.
        return _refuse(
            "the app's settings do not load: run `uv run sandloom config dump` "
            "to see which setting is wrong"
        )
    problem = _token.local_refusal(
        loaded.environment.value,
        loaded.database_url,
        _errors.environment().get("PGHOST"),
        pghostaddr=_errors.environment().get("PGHOSTADDR"),
        pgservice=_errors.environment().get("PGSERVICE"),
    )
    if problem is not None:
        return _refuse(problem)
    try:
        core = load_composition_core(project_root=Path.cwd())
    except (CompositionRootError, ImportError) as exc:
        return _refuse(
            f"cannot import the composition root: {drop_urls(str(exc))}. Run "
            "the command in the app's directory, or set "
            "SANDLOOM_COMPOSITION_ROOT in .env"
        )
    policy = core.claim_authorities
    tenant = request["tenant"]
    ttl = request["ttl_minutes"]  # None: the default; 0 is out of bounds, not "default"
    try:
        minted = mint_dev_token(
            policy,
            user_id=UUID(request["user"]),
            tenant_id=None if tenant is None else UUID(tenant),
            roles=request["roles"],
            ttl_minutes=DEFAULT_TTL_MINUTES if ttl is None else ttl,
        )
    except DevTokenRefused as exc:
        return _refuse(str(exc))
    return _emit(
        {
            "token": minted.token,
            "summary": _summary(minted),
            "notes": _notes(
                minted,
                tenancy_mode=loaded.tenancy_mode,
                tenant_authority=bool(policy.accepted.get("tenant_ids")),
            ),
        }
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
