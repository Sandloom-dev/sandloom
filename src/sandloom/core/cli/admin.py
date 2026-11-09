"""``sandloom admin`` — the console's operator commands.

    sandloom admin export --output FILE [--check]
        render the static admin manifest the composed console module declares
        (module declarations + public OpenAPI), as ``sandloom generate`` does
    sandloom admin create --email ADDRESS [--name NAME]
                          [--invite | --password-stdin] [--if-missing] [--take-over]
                          [--quiet]
        create a platform administrator. At a terminal it asks for the
        password twice; ``--invite`` prints a single-use set-password link
        (15 minutes) instead; ``--password-stdin`` reads one line from a pipe.
        ``--quiet`` prints no success line (``sandloom new`` runs it on the
        terminal after its setup and says it in its own summary).
        ``--if-missing`` exits 0 when the address is already an administrator.
        ``--take-over`` makes an unverified account that holds the address an
        administrator and invalidates its password.
    sandloom admin invite --email ADDRESS
        a new set-password link for an administrator (the older link and every
        session end)
    sandloom admin list [--confirm-production --reason WHY [--show-emails]]
        the platform administrators: address, whether a password is set, the
        last sign-in. Read-only. In production it needs --confirm-production
        and --reason, writes an audit record (identity.admin.listed) and masks
        the addresses unless --show-emails (recorded too). Whoever runs the CLI
        on a server can read the database anyway: the command adds no access,
        only an audit trail and addresses kept out of shell history and logs.

``create`` and ``invite`` act at once outside production; with
``ENVIRONMENT=prod`` they need ``--apply --confirm-production --reason``. No
password is a flag or a file.

Who answers (:mod:`sandloom.core.cli.operator_owner`): two owners, each
read from the composition root's claim map and confirmed by describe before
the first side effect (local, or through ``SERVICE_BINDINGS`` with an
``RPC_ALLOWED_CALLS`` grant, so a CLI process without them works too):

* the ``issuer`` (``<issuer>.security.v1``): the account by address
  (``account_by_email``), the accounts of a listing (``accounts_by_id``), the
  password policy (``password_problem``) and the set-password link
  (``set_password_token``, ``set_password_with_token``,
  ``reissue_set_password``);
* the authority of ``roles`` (``<owner>.security.v1``): whether a user is a
  platform administrator (``is_platform_admin``) and who is
  (``platform_admin_ids``, ``record_admin_listing`` in production).

``create`` makes the account and its role in one unit of work
(``create_platform_admin``, ``take_over_platform_admin``), and ``invite``
mints a link the issuer gives only to an administrator it knows: both need
one module that is the issuer and the roles owner. With two owners they are
refused (SANDLOOM-B039) before anything is read or written, never half done:
create the account at the issuer, then ``sandloom security grant-admin``.
``list`` joins the roles owner's holders with the issuer's addresses, any
two owners. A missing method, binding or grant refuses the same way.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import sandloom.core.cli.generation_input as _generation_input
import sandloom.core.cli.operator_owner as _operator_owner
import sandloom.core.cli.ui as _ui
from sandloom.core.cli._operator import _login_name, add_operator_arguments
from sandloom.core.cli.dev import commands as _dev_commands
from sandloom.core.cli.dev import errors as _dev_errors
from sandloom.core.cli.dev.identity import app_name, project_root
from sandloom.core.cli.operator_owner import (
    OperatorCapabilityUnavailable,
    OperatorOwner,
    OperatorOwnerSubstitution,
)
from sandloom.core.composition import load_composition_core
from sandloom.core.config.settings import settings
from sandloom.core.module_api._errors import ContractCallError
from sandloom.core.route_markers import route_module
from sandloom.core.rpc.errors import RemoteServiceUnavailable

#: Where the link points outside production when FRONTEND_URL is not set and
#: neither a live ``dev run`` nor ``dev up``'s app port says otherwise.
DEV_BASE_URL = "http://127.0.0.1:8000"
#: ``FRONTEND_URL``'s default: a frontend dev server, not this app's console.
_FRONTEND_DEFAULT = "http://localhost:3000"
#: How often the prompt asks before giving up.
PROMPT_ATTEMPTS = 3


class _Refused(Exception):
    """A one-line refusal: exit status 1, the message on stderr."""


@dataclass(frozen=True, slots=True)
class _Intent:
    operator: str
    reason: str


def _build_core() -> Any:
    return load_composition_core()


def _stdout_is_tty() -> bool:
    return sys.stdout.isatty()


def _stdin_is_tty() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    return bool(isatty and isatty())


def _intent(args: argparse.Namespace, default_reason: str) -> _Intent:
    """Production needs the three flags; elsewhere the command acts at once."""
    reason = (args.reason or "").strip()
    operator = (args.operator or _login_name()).strip() or "cli"
    if settings.is_prod and not (
        args.apply and args.confirm_production and len(reason) >= 3
    ):
        raise SystemExit(
            "error: ENVIRONMENT=prod: add --apply --confirm-production "
            '--reason "<why>" (stored in the audit log)'
        )
    return _Intent(operator=f"cli:{operator}", reason=reason or default_reason)


def _dev_run_base(root: Path) -> str | None:
    """Where this checkout's app answers: its live ``sandloom dev run``, else
    the port ``sandloom dev up`` chose for it (``dev run``'s default)."""
    return _dev_commands.app_url(root, _dev_errors.environment())


def _checkout_root() -> Path:
    """The app's checkout: the nearest directory above the working directory
    holding ``pyproject.toml`` (``sandloom dev run`` writes its run files
    there), else the working directory."""
    return project_root(Path.cwd())


def _frontend_url() -> str | None:
    """``FRONTEND_URL`` when it was set to something other than its default."""
    if "frontend_url" not in settings.model_fields_set:
        return None
    url = (settings.frontend_url or "").rstrip("/")
    return url if url and url != _FRONTEND_DEFAULT else None


#: The name a module gives the route of the page that opens a set-password
#: link while it serves it (ADR-0015: the composed app's console route, not a
#: module's setting). A module that serves no such page is the reset email's
#: frontend case.
LINK_PAGE_ROUTE = "set_password_page"
#: The path the console answers at (the start page's probe): the module that
#: mounted it is the console module, the only one whose page a link opens.
CONSOLE_ENTRY = "/admin"


def _gets(route: Any) -> bool:
    return "GET" in (getattr(route, "methods", None) or ())


def _console_module(routes: Sequence[Any]) -> str | None:
    """The module that mounted the console's entry page (the import path or
    entry-point name the loader registered it under, stamped on each route it
    mounts), else ``None``: no module serves it, or more than one claims it."""
    owners = {
        route_module(route)
        for route in routes
        if _gets(route) and str(getattr(route, "path", "")).rstrip("/") == CONSOLE_ENTRY
    }
    return next(iter(owners)) if len(owners) == 1 else None


def _page_path(core: Any) -> str | None:
    """The path of the set-password page the composed application's console
    serves, or ``None``.

    A link carries a single-use token, so it opens only a mounted ``GET``
    route of that name (never a pattern) that the console module mounted:
    a route of that name another module or the application mounts is not
    the console's page (``None``: the frontend's page, else a refusal). Two
    routes of that name are refused before anything is done: which one a
    link would open is ambiguous.
    """
    routes = list(getattr(getattr(core, "app", None), "routes", ()))
    named = [r for r in routes if getattr(r, "name", None) == LINK_PAGE_ROUTE]
    if len(named) > 1:
        where = ", ".join(
            sorted(
                f"{getattr(r, 'path', '?')} (mounted by "
                f"{route_module(r) or 'the application'})"
                for r in named
            )
        )
        raise SystemExit(
            f"error: {len(named)} routes are named {LINK_PAGE_ROUTE!r}: "
            f"{where}. A set-password link carries a single-use token and "
            "opens only the console's page: rename the other routes"
        )
    console = _console_module(routes)
    for route in named:
        path = str(getattr(route, "path", ""))
        if (
            console is not None
            and route_module(route) == console
            and _gets(route)
            and "{" not in path
        ):
            return path
    return None


def _base_url() -> str:
    """Where this application is reached: ``FRONTEND_URL`` when set, else (outside
    production) its live ``dev run`` or the default local address."""
    base = _frontend_url()
    if base is not None:
        return base
    if settings.is_prod:
        raise SystemExit(
            "error: set FRONTEND_URL (where this app is served) so the "
            "link points at the right host"
        )
    return _dev_run_base(_checkout_root()) or DEV_BASE_URL


def link_for(core: Any, token: str, email: str = "") -> str:
    """Where the set-password link points.

    The page comes from ``core``'s composed application: a served set-password
    route (``LINK_PAGE_ROUTE``), else the frontend's reset page
    (``FRONTEND_URL``), else a refusal. The token (and the address the page
    pre-fills) is always in the URL fragment, which a browser never sends: no
    server, proxy or referrer log records it. A custom frontend serving
    ``/auth/reset-password`` reads it from ``location.hash``
    (``docs/admin-console/index.md``).
    """
    page = _page_path(core)
    if page is not None:
        fragment = f"token={token}" + (f"&email={quote(email)}" if email else "")
        return f"{_base_url()}{page}#{fragment}"
    frontend = _frontend_url()
    if frontend is None:
        raise SystemExit(
            "error: this application serves no set-password page: set "
            "FRONTEND_URL to a frontend that serves /auth/reset-password, or "
            "enable the console"
        )
    # The page the reset emails open, with the token in the fragment.
    return f"{frontend}/auth/reset-password#token={token}"


def _check_link_target(core: Any) -> None:
    """Refuse before acting when no link could be printed."""
    link_for(core, "")


@dataclass(frozen=True, slots=True)
class _Side:
    """One confirmed owner: who it is and the contract to call."""

    owner: OperatorOwner
    contract: Any

    async def call(self, core: Any, method: str, *args: Any) -> Any:
        """One call of a confirmed method; the provider's 403 of a caller its
        ``RPC_ALLOWED_CALLS`` does not list is named as the missing grant."""
        try:
            return await getattr(self.contract, method)(*args)
        except (ContractCallError, RemoteServiceUnavailable) as exc:
            if (
                isinstance(exc, RemoteServiceUnavailable)
                and getattr(exc, "code", None) == "rpc_caller_forbidden"
            ):
                raise _operator_owner.forbidden_refusal(
                    core, self.owner, method
                ) from exc
            raise


@dataclass(frozen=True, slots=True)
class AdminOwners:
    """The two owners of a ``sandloom admin`` / ``grant-admin`` command,
    each confirmed: the ``issuer`` (accounts, credentials, the lookup by
    address) and the authority of ``roles`` (the platform ``admin`` role).
    One module may be both."""

    issuer: _Side
    roles: _Side

    @property
    def one_module(self) -> bool:
        """The issuer and the roles owner are the same module."""
        return self.issuer.owner.path == self.roles.owner.path


def _two_owners(command: str, issuer: OperatorOwner, roles: OperatorOwner) -> str:
    what = (
        "mints a set-password link the issuer gives only to an administrator it knows"
        if command == "admin invite"
        else "creates the account and its platform role in one unit of work"
    )
    then = (
        "reset the administrator's password at the issuer"
        if command == "admin invite"
        else "create the account at the issuer (its sign-up, or the external "
        "provider), then: uv run sandloom security grant-admin --email "
        '<address> --apply --reason "<why>"'
    )
    return (
        f"{command}: {issuer.label()} and {roles.label()} are different "
        f"modules; {command} {what}, which needs one module that is both, so "
        f"nothing was done: {then}"
    )


async def admin_owners(
    core: Any,
    command: str,
    *,
    issuer: Sequence[str],
    roles: Sequence[str],
    together: Sequence[str] = (),
) -> AdminOwners:
    """The issuer and the roles owner of ``core``'s composition, each
    confirmed to provide its methods (``issuer`` / ``roles``), or a refusal
    (SANDLOOM-B039 / B040, exit 1) before anything is read or written.

    ``together``: methods that act on the account and the role in one unit
    of work; they need one module that is both owners (two owners: refused
    first, before any describe), and are confirmed on it.
    """
    issuer_owner = _operator_owner.operator_owner(core, "user_lookup", command=command)
    roles_owner = _operator_owner.operator_owner(
        core, "platform_admin", command=command
    )
    if together and issuer_owner.path != roles_owner.path:
        raise OperatorCapabilityUnavailable(
            _two_owners(command, issuer_owner, roles_owner)
        )
    issued = await _operator_owner.confirm_operator_capability(
        core, issuer_owner, [*issuer, *together]
    )
    held = await _operator_owner.confirm_operator_capability(core, roles_owner, roles)
    return AdminOwners(
        issuer=_Side(issuer_owner, issued), roles=_Side(roles_owner, held)
    )


def _field(answer: Any, name: str) -> Any:
    """A field of a DTO answer (a model, or a mapping off the wire)."""
    if isinstance(answer, Mapping):
        return answer.get(name)
    return getattr(answer, name, None)


@dataclass(frozen=True, slots=True)
class Standing:
    """An account as an admin command sees it: the issuer's account joined
    with the roles owner's answer."""

    user_id: Any
    email_verified: bool
    is_active: bool
    is_platform_admin: bool


async def account_standing(
    core: Any, owners: AdminOwners, email: str, *, with_role: bool = True
) -> Standing | None:
    """The issuer's account with this address and, with ``with_role``,
    whether the roles owner says it is a platform administrator; ``None``
    without an account."""
    account = await owners.issuer.call(core, "account_by_email", email)
    if account is None:
        return None
    user_id = _field(account, "user_id")
    active = _field(account, "is_active")
    return Standing(
        user_id=user_id,
        email_verified=bool(_field(account, "email_verified")),
        is_active=True if active is None else bool(active),
        is_platform_admin=bool(
            with_role and await owners.roles.call(core, "is_platform_admin", user_id)
        ),
    )


def _key(command: str) -> str:
    # A fresh key per run: running the command again must act, not replay.
    return f"sandloom-admin:{command}:{uuid.uuid4().hex}"


def _style() -> _ui.Style:
    """stdout's look: colour at a terminal, the same text without."""
    return _ui.style_for(sys.stdout, _dev_errors.environment())


def _print_link(core: Any, email: str, token: str, *, created: str) -> None:
    style = _style()
    print(
        f"{style.green(created)} Set the password within 15 minutes:\n"
        f"  {style.accent(link_for(core, token, email))}\n"
        "The link works once. A new one: "
        f"uv run sandloom admin invite --email {email}"
    )


# ------------------------------------------------------------- passwords ----


def _mode(args: argparse.Namespace) -> str:
    if args.invite and args.password_stdin:
        raise SystemExit("error: choose one of --invite and --password-stdin")
    if args.invite:
        return "invite"
    if args.password_stdin:
        if _stdin_is_tty():
            raise SystemExit(
                "error: --password-stdin reads a pipe, and stdin is a terminal: "
                "leave the flag out to be asked for the password"
            )
        return "stdin"
    if _stdin_is_tty() and _stdout_is_tty():
        return "prompt"
    raise SystemExit(
        "error: no terminal to ask for the password: add --invite (prints a "
        "single-use set-password link) or --password-stdin (reads it from a pipe)"
    )


#: The issuer's methods a mode needs besides the lookup and the link.
_PASSWORD_METHODS = ("password_problem", "set_password_with_token")


async def _read_password(
    core: Any,
    issuer: _Side,
    email: str,
    mode: str,
    ask: Callable[[str], str],
) -> str | None:
    """The password to set, or ``None`` for an invite.

    The issuer's ``password_problem`` answer over ``<issuer>.security.v1`` is
    the only check: the kernel holds no password policy of its own (ADR-0012
    decision 6), so the prompt states no length either.
    """
    if mode == "invite":
        return None
    if mode == "stdin":
        password = sys.stdin.readline().rstrip("\r\n")
        problem = await issuer.call(core, "password_problem", email, password)
        if problem:
            raise SystemExit(f"error: {problem}")
        return password
    app = app_name(_checkout_root())
    prompt = f"Password for {email} (console administrator of {app}): "
    for _ in range(PROMPT_ATTEMPTS):
        password = ask(prompt)
        problem = await issuer.call(core, "password_problem", email, password)
        if problem:
            print(f"{problem}. Try again.", file=sys.stderr)
            continue
        if ask("Repeat the password: ") != password:
            print("The passwords do not match. Try again.", file=sys.stderr)
            continue
        return password
    raise SystemExit("error: no password set; nothing was created")


# --------------------------------------------------------------- commands ----


def _exists_message(email: str, standing: Any) -> str:
    if standing.is_platform_admin:
        return (
            f"{email} already exists and is an administrator. A new password: "
            f"uv run sandloom admin invite --email {email}"
        )
    if not standing.email_verified:
        return (
            f"{email} already exists and its address is not verified: to make it "
            f"an administrator and end its password, add --take-over"
        )
    return (
        f"{email} already exists. To make it an administrator: uv run sandloom "
        f'security grant-admin --email {email} --apply --reason "<why>"; a new '
        f"password: uv run sandloom admin invite --email {email}"
    )


def _check_existing(email: str, standing: Any, args: argparse.Namespace) -> bool:
    """Whether to stop with exit 0 (``--if-missing``); refuses what may not proceed."""
    if standing is None:
        return False
    if standing.is_platform_admin and args.if_missing:
        print(f"{email} is already an administrator; nothing changed.")
        return True
    if standing.is_platform_admin or not args.take_over:
        raise _Refused(_exists_message(email, standing))
    if standing.email_verified:
        raise _Refused(
            f"{email} is a verified account: --take-over is for an "
            f"unverified one. {_exists_message(email, standing)}"
        )
    return False


async def _new_admin(
    core: Any,
    owners: AdminOwners,
    email: str,
    args: argparse.Namespace,
    intent: _Intent,
) -> Any:
    """The new administrator's id, or ``None`` when another run made it first
    and ``--if-missing`` allows that."""
    try:
        return await owners.issuer.call(
            core,
            "create_platform_admin",
            email,
            args.name or "",
            intent.reason,
            intent.operator,
            _key("create"),
        )
    except ContractCallError as exc:
        if exc.status_code != 409:
            raise
        # Another run created it first (one user per address).
        raced = await account_standing(core, owners, email)
        if raced is not None and raced.is_platform_admin and args.if_missing:
            print(f"{email} is already an administrator; nothing changed.")
            return None
        raise _Refused(
            _exists_message(email, raced) if raced else str(exc.detail)
        ) from exc


def _mode_unless_missing(args: argparse.Namespace) -> str | None:
    """:func:`_mode`; with ``--if-missing`` its refusal waits (``None``): an
    existing administrator needs no password, so the mode is asked for only
    once something will be created."""
    try:
        return _mode(args)
    except SystemExit:
        if not args.if_missing:
            raise
        return None


def _create_methods(args: argparse.Namespace, mode: str | None) -> list[str]:
    """Everything ``admin create`` may call on the account side, confirmed
    before the first read: the account and its role are made in one unit
    (one module that is the issuer and the roles owner)."""
    methods = ["create_platform_admin", "set_password_token"]
    if args.take_over:
        methods.append("take_over_platform_admin")
    if mode in ("stdin", "prompt"):
        methods.extend(_PASSWORD_METHODS)
    return methods


async def create_admin(
    core: Any, args: argparse.Namespace, *, ask: Callable[[str], str]
) -> int:
    """``sandloom admin create``; returns the exit status."""
    email = args.email.strip()
    mode = _mode_unless_missing(args)
    intent = _intent(args, "first administrator")
    if mode == "invite":
        _check_link_target(core)
    owners = await admin_owners(
        core,
        "admin create",
        issuer=["account_by_email"],
        roles=["is_platform_admin"],
        together=_create_methods(args, mode),
    )
    standing = await account_standing(core, owners, email)
    if _check_existing(email, standing, args):
        return 0
    mode = mode or _mode(args)
    password = await _read_password(core, owners.issuer, email, mode, ask)
    if standing is not None:
        await owners.issuer.call(
            core,
            "take_over_platform_admin",
            standing.user_id,
            intent.reason,
            intent.operator,
            _key("take-over"),
        )
        user_id = standing.user_id
        done = f"{email} is now an administrator; its old password no longer works."
    else:
        user_id = await _new_admin(core, owners, email, args, intent)
        if user_id is None:
            return 0
        done = f"Administrator {email} created."
    token = await owners.issuer.call(core, "set_password_token", user_id)
    if not token:
        raise _Refused(
            f"{owners.issuer.owner.module} issued no set-password link for {email}"
        )
    if password is None:
        _print_link(core, email, token, created=done)
        return 0
    try:
        await owners.issuer.call(
            core, "set_password_with_token", token, password, _key("password")
        )
    except ContractCallError as exc:
        # No link here: the user did not ask for one (--invite), and a
        # terminal's scrollback is no place for a usable token.
        print(
            f"{done} The password was not set ({exc.detail}). A set-password "
            f"link: uv run sandloom admin invite --email {email}",
            file=sys.stderr,
        )
        return 1
    if not args.quiet:
        print(f"{_style().green(done)} {email} can sign in at {_sign_in_hint(core)}")
    return 0


def _sign_in_hint(core: Any) -> str:
    page = _page_path(core)
    if page is None:
        return "the console"
    try:
        return f"{_base_url()}{page.rsplit('/', 1)[0]}/"
    except SystemExit:
        return "the console"


def mask_email(email: str) -> str:
    """``vasiliy@gmail.com`` -> ``v***y@gmail.com`` (the domain stays)."""
    local, at, domain = email.partition("@")
    if len(local) <= 2:
        hidden = (local[:1] or "") + "***"
    else:
        hidden = f"{local[0]}***{local[-1]}"
    return hidden + at + domain


def _listing_intent(args: argparse.Namespace) -> _Intent | None:
    """Production: ``--confirm-production --reason`` (an audited read);
    elsewhere ``None`` (no flags, no audit)."""
    if not settings.is_prod:
        return None
    reason = (args.reason or "").strip()
    if not (args.confirm_production and len(reason) >= 3):
        raise SystemExit(
            "error: ENVIRONMENT=prod: add --confirm-production --reason "
            '"<why>" (the listing is recorded in the audit log)'
        )
    operator = (args.operator or _login_name()).strip() or "cli"
    return _Intent(operator=f"cli:{operator}", reason=reason)


def _utc(moment: datetime) -> str:
    """A sign-in time in UTC, whatever zone the driver returned it in (a
    naive value is already UTC)."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _listing_row(account: Any, *, show: bool) -> tuple[str, str, str]:
    """One line of ``admin list`` from the issuer's account: an issuer that
    keeps no passwords (an external provider) says neither set nor not set,
    and one without a last sign-in says ``never``."""
    email = str(_field(account, "email") or "")
    password_set = _field(account, "password_set")
    active = _field(account, "is_active")
    password = (
        "at the provider"
        if password_set is None
        else ("set" if password_set else "not set")
    ) + ("" if active is None or active else " (deactivated)")
    last = _field(account, "last_login_at")
    if isinstance(last, str):
        last = datetime.fromisoformat(last)
    return (
        email if show else mask_email(email),
        password,
        _utc(last) if last else "never",
    )


async def list_admins(core: Any, args: argparse.Namespace) -> int:
    """``sandloom admin list``; returns the exit status."""
    intent = _listing_intent(args)
    owners = await admin_owners(
        core,
        "admin list",
        issuer=["accounts_by_id"],
        roles=["platform_admin_ids"]
        + ([] if intent is None else ["record_admin_listing"]),
    )
    show = intent is None or bool(args.show_emails)
    if intent is not None:
        await owners.roles.call(
            core,
            "record_admin_listing",
            intent.reason,
            intent.operator,
            bool(args.show_emails),
            _key("list"),
        )
    ids = list(await owners.roles.call(core, "platform_admin_ids"))
    admins = await owners.issuer.call(core, "accounts_by_id", ids) if ids else []
    if not admins:
        print(
            "No administrator yet. Create one: uv run sandloom admin create "
            "--email you@example.com"
        )
        return 0
    rows = [_listing_row(admin, show=show) for admin in admins]
    widths = [
        max(len(r[i]) for r in [("email", "password", "last sign-in"), *rows])
        for i in range(3)
    ]
    for row in [("email", "password", "last sign-in"), *rows]:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    print(
        "A new password (and the end of every session): "
        "uv run sandloom admin invite --email <address>"
    )
    return 0


async def invite_admin(core: Any, args: argparse.Namespace) -> int:
    """``sandloom admin invite``; returns the exit status."""
    email = args.email.strip()
    intent = _intent(args, "new set-password link")
    _check_link_target(core)
    owners = await admin_owners(
        core,
        "admin invite",
        issuer=["account_by_email"],
        roles=["is_platform_admin"],
        together=["reissue_set_password", "set_password_token"],
    )
    standing = await account_standing(core, owners, email)
    if standing is None:
        raise _Refused(
            f"no account {email}: create it with "
            f"uv run sandloom admin create --email {email}"
        )
    if not standing.is_platform_admin:
        raise _Refused(
            f"{email} is not a platform administrator: uv run sandloom security "
            f'grant-admin --email {email} --apply --reason "<why>"'
        )
    try:
        await owners.issuer.call(
            core,
            "reissue_set_password",
            standing.user_id,
            intent.reason,
            intent.operator,
            _key("invite"),
        )
    except ContractCallError as exc:
        raise _Refused(f"{email}: {exc.detail}") from exc
    token = await owners.issuer.call(core, "set_password_token", standing.user_id)
    if not token:
        raise _Refused(
            f"{owners.issuer.owner.module} issued no set-password link for {email}"
        )
    _print_link(
        core, email, token, created=f"A new link for {email}; its sessions ended."
    )
    return 0


def _exported(app: Any, name: str) -> str:
    """The text of the generated file the composition declares under ``name``,
    or of the only one it declares (the console's manifest)."""
    files = _generation_input.declared(app)
    chosen = [f for f in files if f.name == name] or (files if len(files) == 1 else [])
    if not chosen:
        declared = ", ".join(sorted(f.name for f in files)) or "none"
        raise ValueError(
            f"no composed module declares a generated file {name!r} (declared: "
            f"{declared}); compose the console module in the build-time composition"
        )
    [item] = chosen[:1]
    if item.problems:
        raise ValueError("; ".join(item.problems))
    return item.content


def _export(args: argparse.Namespace) -> int:
    from sandloom.core.composition import load_composition_app  # noqa: PLC0415 - cold: static command - only the export builds the app and settings; --help works unconfigured

    try:
        payload = _exported(load_composition_app(), args.output.name)
        if args.check:
            if not args.output.exists() or args.output.read_text() != payload:
                raise ValueError("admin manifest is stale; run sandloom admin export")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload)
    except (ValueError, KeyError, AttributeError, ImportError) as exc:
        print(f"sandloom admin: {exc}", file=sys.stderr)
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandloom admin",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="compile a static admin manifest")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--check", action="store_true", help="fail on manifest drift")

    create = commands.add_parser("create", help="create a platform administrator")
    create.add_argument("--email", required=True, help="the administrator's address")
    create.add_argument("--name", default=None, help="display name")
    create.add_argument(
        "--invite",
        action="store_true",
        help="print a single-use set-password link (15 minutes) instead of asking",
    )
    create.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from one line of stdin (a pipe, not a terminal)",
    )
    create.add_argument(
        "--if-missing",
        action="store_true",
        help="exit 0 without changes when the address is already an administrator",
    )
    create.add_argument(
        "--take-over",
        action="store_true",
        help="an unverified account holds the address: make it an administrator "
        "and invalidate its password",
    )
    create.add_argument(
        "--quiet",
        action="store_true",
        help="no success line (errors and an --invite link still print)",
    )
    add_operator_arguments(create)

    invite = commands.add_parser(
        "invite", help="a new set-password link for an administrator"
    )
    invite.add_argument("--email", required=True, help="the administrator's address")
    add_operator_arguments(invite)

    listing = commands.add_parser(
        "list", help="the platform administrators and whether each has a password"
    )
    listing.add_argument(
        "--confirm-production",
        action="store_true",
        help="required when ENVIRONMENT=prod (the listing is audited)",
    )
    listing.add_argument("--reason", default="", help="why (stored in the audit log)")
    listing.add_argument(
        "--operator", default=None, help="who asks (default: the OS login name)"
    )
    listing.add_argument(
        "--show-emails",
        action="store_true",
        help="production: the full addresses (masked by default; recorded)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "export":
        return _export(args)
    try:
        if args.command == "list":
            return asyncio.run(list_admins(_build_core(), args))
        if args.command == "create":
            return asyncio.run(
                create_admin(
                    _build_core(), args, ask=lambda prompt: getpass.getpass(prompt)
                )
            )
        return asyncio.run(invite_admin(_build_core(), args))
    except _Refused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OperatorCapabilityUnavailable, OperatorOwnerSubstitution) as exc:
        # Refused before anything was done (exit 1, like sandloom security).
        print(exc.code, file=sys.stderr)
        return 1
    except ContractCallError as exc:
        print(f"error: the owner refused: {exc.detail}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise
    except KeyboardInterrupt:
        # Ctrl-C at the password prompt (the `sandloom new` wizard runs this
        # command on the terminal): one line, never a traceback.
        print("\nCancelled.", file=sys.stderr)
        return 130


__all__ = [
    "AdminOwners",
    "Standing",
    "account_standing",
    "admin_owners",
    "create_admin",
    "invite_admin",
    "link_for",
    "list_admins",
    "main",
    "mask_email",
]
