"""``sandloom dev token``: an access token for your own routes, development only.

    sandloom dev token [--user UUID] [--tenant UUID] [--role NAME]...
                       [--ttl-minutes N] [--quiet]

Prints one token on stdout (``TOKEN=$(uv run sandloom dev token)``) and what it
carries on stderr. The app accepts it like a token of its own issuer: it is
signed with the app's key and carries the provenance the app's claim policy
asks for (:mod:`sandloom.core.auth.dev_token`), which a hand-signed JWT lacks.

Because that is a forgery of provenance, the command refuses before anything
of the app is imported (this module) and again once the app's settings are
loaded (:mod:`sandloom.core.cli.dev.token_child`, in a child process, as the
composition is read elsewhere in ``sandloom dev``):

* ``ENVIRONMENT`` must be exactly ``dev`` or ``test``, exported or in ``.env``.
  Not set, ``prod``, a typo (``Dev``) and every other spelling are refused: the
  app itself runs as ``dev`` when it is not set but refuses a dev token then
  (:func:`sandloom.core.auth.tokens.dev_environment_problem`), so a minting tool
  must not guess;
* every host of ``DATABASE_URL`` (read as SQLAlchemy reads it: its authority,
  its ``host`` / ``hostaddr`` query parameters, and the ``PGHOST``,
  ``PGHOSTADDR`` and ``PGSERVICE`` of the process environment where the url
  names none) must be this machine: loopback, ``localhost`` or a unix socket;
  a file database has no host and passes. A copied production ``.env`` that
  still says ``ENVIRONMENT=dev`` carries a remote database, and this is the
  early refusal of it. It is no protection for the key: the app's own verifier
  refuses every token this command mints unless it too has ``ENVIRONMENT`` set
  to ``dev`` or ``test`` (the ``dev_mint`` marker,
  :mod:`sandloom.core.auth.dev_token`);
* not with ``CI`` set to a true value (``1``, ``true``, ``yes`` or ``on``; the
  same reading ``sandloom dev up`` gives it).

``ENVIRONMENT``, ``DATABASE_URL``, ``PGHOST``, ``PGHOSTADDR`` and ``PGSERVICE``
are read raw here, not through
``Settings``, on purpose: this gate runs before any code of the app is
imported, and the loaded settings repeat it in the child process.

No secret is read here, passed on or printed: the child process signs, and its
stderr is shown only with ``--debug``, without urls.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from dotenv import dotenv_values

from sandloom.core.cli.dev import commands as _commands
from sandloom.core.cli.dev import envfile as _envfile
from sandloom.core.cli.dev import process as _process
from sandloom.core.cli.url_text import drop_urls
from sandloom.core.config.common import Environment
from sandloom.core.sql_alchemy.url_hosts import database_hosts, remote_database_target

#: The only values of ``ENVIRONMENT`` a dev token is minted under. The kernel's
#: mint and verifier hold the same list
#: (``sandloom.core.auth.tokens.DEV_ENVIRONMENTS``; a test keeps the two equal).
DEV_ENVIRONMENTS: tuple[str, ...] = (Environment.dev.value, Environment.test.value)

#: The stable subject of a dev token: what one token creates, the next finds.
DEV_USER_ID = UUID("00000000-0000-4000-8000-0000000000d1")

#: Prefix of the child's answer line (the app may print at import).
SENTINEL = "SANDLOOM-DEV-TOKEN:"

_SHOWN_ENVIRONMENT = 24


def uuid_argument(text: str) -> UUID:
    """The ``type`` of ``--user`` and ``--tenant``."""
    try:
        return UUID(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text[:40]!r} is not a UUID") from exc


def local_refusal(
    environment: str | None,
    database_url: str | None,
    pghost: str | None = None,
    *,
    pghostaddr: str | None = None,
    pgservice: str | None = None,
) -> str | None:
    """Why a dev token is not minted for this ``ENVIRONMENT`` and
    ``DATABASE_URL`` (``PGHOST``, ``PGHOSTADDR`` and ``PGSERVICE`` are what
    libpq fills in where the url names no host or address), or ``None``. One
    rule for the raw values (before the app is imported) and for the loaded
    settings (in the child)."""
    if not environment:
        return (
            "ENVIRONMENT is not set: `sandloom dev token` runs only with "
            "ENVIRONMENT=dev or ENVIRONMENT=test, set explicitly (the generated "
            ".env has ENVIRONMENT=dev)"
        )
    if environment not in DEV_ENVIRONMENTS:
        return (
            f"ENVIRONMENT={environment[:_SHOWN_ENVIRONMENT]!r}: `sandloom dev "
            "token` runs only with ENVIRONMENT=dev or ENVIRONMENT=test, spelled "
            "exactly so"
        )
    if not database_url:
        return (
            "DATABASE_URL is not set (exported or in .env): run "
            "`uv run sandloom dev up` first"
        )
    try:
        remote = remote_database_target(
            database_url, pghost, pghostaddr=pghostaddr, pgservice=pgservice
        )
    except ValueError:
        return "DATABASE_URL does not parse as a url: fix it before minting a token"
    if remote is not None:
        return (
            f"DATABASE_URL points at {remote}, which is not this machine: "
            "`sandloom dev token` runs only against a database on 127.0.0.1, "
            "localhost, a unix socket or a file (a token signed with this "
            "app's key is valid wherever that key is used)"
        )
    return None


def _from_environ(environ: Mapping[str, str], name: str) -> list[str]:
    """Every value the process environment gives ``name``, whatever the case
    of the variable (the settings reader ignores case)."""
    return [value for key, value in environ.items() if key.upper() == name]


def _raw_setting(
    environ: Mapping[str, str], dotenv: Mapping[str, str | None], name: str
) -> str | None:
    """``name`` as the app reads it: the environment, else ``.env``, in either
    place whatever the case of the name (the settings reader ignores case; an
    entry with no value is no entry). When one place spells it in several
    cases, a value that would be refused wins (the loaded settings decide in
    the child, this only refuses early)."""
    values = _from_environ(environ, name) or [
        value
        for key, value in dotenv.items()
        if key.upper() == name and value is not None
    ]
    if not values:
        return None
    bad = [v for v in values if name != "ENVIRONMENT" or v not in DEV_ENVIRONMENTS]
    return (bad or values)[0]


def raw_refusal(environ: Mapping[str, str], dotenv_text: str) -> str | None:
    """:func:`local_refusal` of the ``ENVIRONMENT`` and ``DATABASE_URL`` the
    process environment and ``.env`` give."""
    dotenv = dotenv_values(stream=io.StringIO(dotenv_text))
    return local_refusal(
        _raw_setting(environ, dotenv, "ENVIRONMENT"),
        _raw_setting(environ, dotenv, "DATABASE_URL"),
        environ.get("PGHOST"),
        pghostaddr=environ.get("PGHOSTADDR"),
        pgservice=environ.get("PGSERVICE"),
    )


@dataclass(frozen=True, slots=True)
class Request:
    """What ``sandloom dev token`` was asked for (``None``: the default)."""

    user: UUID | None = None
    tenant: UUID | None = None
    roles: Sequence[str] = ()
    ttl_minutes: int | None = None
    quiet: bool = False


def _child_request(request: Request) -> str:
    return json.dumps(
        {
            "user": str(request.user or DEV_USER_ID),
            "tenant": None if request.tenant is None else str(request.tenant),
            "roles": list(request.roles),
            "ttl_minutes": request.ttl_minutes,
        }
    )


def _child_answer(ctx: _commands.Context, request: Request) -> dict[str, object]:
    """The child's answer: it runs in the checkout, where the app's package
    and ``.env`` are, and reads the claim policy of the app's composition."""
    refusal = (
        "cannot mint a token from the composition root: %s. Fix the import "
        "(or SANDLOOM_COMPOSITION_ROOT in .env), then run the command again"
    )
    try:
        proc = _process.run(
            [
                sys.executable,
                "-m",
                "sandloom.core.cli.dev.token_child",
                _child_request(request),
            ],
            env=ctx.environ,
            cwd=ctx.root,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _commands.DevError(refusal % (str(exc) or type(exc).__name__)) from exc
    lines = [
        line.removeprefix(SENTINEL)
        for line in proc.stdout.splitlines()
        if line.startswith(SENTINEL)
    ]
    if proc.returncode != 0 or not lines:
        what = f"it exited with code {proc.returncode}" + (
            "" if lines else " and printed no answer"
        )
        if ctx.verbose:
            # Its last line can echo an input of the settings: no url, which
            # can hold a password anywhere in it.
            tail = (proc.stderr.strip().splitlines() or ["it printed nothing"])[-1]
            what += f": {drop_urls(tail)}"
        else:
            what += (
                " (`uv run sandloom --debug dev token` or `SANDLOOM_DEBUG=1` "
                "shows its last error line)"
            )
        raise _commands.DevError(refusal % what)
    try:
        answer = json.loads(lines[-1])
    except ValueError as exc:
        raise _commands.DevError(refusal % f"its answer is not JSON ({exc})") from exc
    if not isinstance(answer, dict):
        raise _commands.DevError(refusal % "its answer is not an object")
    return answer


def run(ctx: _commands.Context, request: Request) -> int:
    """Print a dev token for ``request`` (exit 0), or refuse (a
    :class:`~sandloom.core.cli.dev.commands.DevError`, exit 1)."""
    if _commands.in_ci(ctx.environ):  # main() refuses first; kept for a direct call
        raise _commands.DevError(CI_REFUSAL)
    try:
        dotenv_text = (ctx.root / _envfile.ENV_FILE).read_text(encoding="utf-8")
    except OSError:
        dotenv_text = ""
    problem = raw_refusal(ctx.environ, dotenv_text)
    if problem is not None:
        raise _commands.DevError(problem)
    answer = _child_answer(ctx, request)
    refused = answer.get("refused")
    if isinstance(refused, str):
        raise _commands.DevError(refused)
    token = answer.get("token")
    summary = answer.get("summary")
    notes = answer.get("notes")
    if not isinstance(token, str) or not isinstance(summary, str):
        raise _commands.DevError("the token process answered without a token")
    print(token, file=ctx.out, flush=True)
    if not request.quiet:
        print(summary, file=ctx.err, flush=True)
        for note in notes if isinstance(notes, list) else []:
            print(f"note: {note}", file=ctx.err, flush=True)
    return 0


#: What ``sandloom dev token`` answers in CI (``main`` refuses before ``run``).
CI_REFUSAL = (
    "`sandloom dev token` is for a developer machine (CI is set); a test mints "
    "its own tokens with `as_user` (sandloom.sdk.testing)"
)

__all__ = [
    "CI_REFUSAL",
    "DEV_ENVIRONMENTS",
    "DEV_USER_ID",
    "SENTINEL",
    "Request",
    "database_hosts",
    "local_refusal",
    "raw_refusal",
    "run",
    "uuid_argument",
]
