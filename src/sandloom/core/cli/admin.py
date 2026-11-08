"""``sandloom admin`` — the console's operator commands.

    sandloom admin export --output FILE [--check]
        compile the static admin manifest (module declarations + public OpenAPI)
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
password is a flag or a file. They call identity's ``identity.security.v1``
contract (local, or through ``SERVICE_BINDINGS`` with an ``RPC_ALLOWED_CALLS``
grant), so they also work when identity is its own service.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import sandloom.core.cli.ui as _ui
from sandloom.core.admin import compile_app
from sandloom.core.cli._operator import _login_name, add_operator_arguments
from sandloom.core.cli.dev import commands as _dev_commands
from sandloom.core.cli.dev import errors as _dev_errors
from sandloom.core.cli.dev.identity import app_name, project_root
from sandloom.core.composition import load_composition_core
from sandloom.core.config.settings import settings
from sandloom.core.module_api._errors import ContractCallError
from sandloom.core.module_api._service_registry import ServiceNotFound

#: The token issuer's operator contract (provided by ``sandloom_identity``).
SECURITY_CONTRACT = "identity.security.v1"
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


def _console_enabled() -> bool:
    enabled = settings.admin_console_enabled
    return (not settings.is_prod) if enabled is None else bool(enabled)


def link_for(token: str, email: str = "") -> str:
    """Where the set-password link points.

    The token (and the address the console pre-fills) is always in the URL
    fragment, which a browser never sends: no server, proxy or referrer log
    records it. A custom frontend serving ``/auth/reset-password`` reads it
    from ``location.hash`` (``docs/admin-console/index.md``).
    """
    frontend = _frontend_url()
    if _console_enabled():
        base = frontend
        if base is None:
            if settings.is_prod:
                raise SystemExit(
                    "error: set FRONTEND_URL (where this app is served) so the "
                    "link points at the right host"
                )
            base = _dev_run_base(_checkout_root()) or DEV_BASE_URL
        fragment = f"token={token}" + (f"&email={quote(email)}" if email else "")
        return f"{base}/admin/set-password#{fragment}"
    if frontend is None:
        raise SystemExit(
            "error: the admin console is off (ADMIN_CONSOLE_ENABLED): set "
            "FRONTEND_URL to a frontend that serves /auth/reset-password, or "
            "enable the console"
        )
    # The page the reset emails open, with the token in the fragment.
    return f"{frontend}/auth/reset-password#token={token}"


def _check_link_target() -> None:
    """Refuse before acting when no link could be printed."""
    link_for("")


def _service(core: Any) -> Any:
    try:
        return core.get_service(SECURITY_CONTRACT)
    except ServiceNotFound as exc:
        raise _Refused(
            f"the identity module is not installed here: {SECURITY_CONTRACT} is "
            "neither local nor bound in SERVICE_BINDINGS"
        ) from exc


async def _call(service: Any, method: str, *args: Any) -> Any:
    """One contract call; an identity service without ``method`` is named."""
    try:
        bound = getattr(service, method)
    except AttributeError as exc:
        raise _Refused(
            f"the identity service has no {SECURITY_CONTRACT}.{method}: upgrade "
            "sandloom-identity (and grant it in RPC_ALLOWED_CALLS)"
        ) from exc
    try:
        return await bound(*args)
    except ContractCallError as exc:
        if exc.status_code == 404:
            raise _Refused(
                f"the identity service has no {SECURITY_CONTRACT}.{method}: "
                "upgrade sandloom-identity (and grant it in RPC_ALLOWED_CALLS)"
            ) from exc
        raise


def _key(command: str) -> str:
    # A fresh key per run: running the command again must act, not replay.
    return f"sandloom-admin:{command}:{uuid.uuid4().hex}"


def _style() -> _ui.Style:
    """stdout's look: colour at a terminal, the same text without."""
    return _ui.style_for(sys.stdout, _dev_errors.environment())


def _print_link(email: str, token: str, *, created: str) -> None:
    style = _style()
    print(
        f"{style.green(created)} Set the password within 15 minutes:\n"
        f"  {style.accent(link_for(token, email))}\n"
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


async def _read_password(
    service: Any, email: str, mode: str, ask: Callable[[str], str]
) -> str | None:
    """The password to set, or ``None`` for an invite.

    The provider's ``password_problem`` answer over ``identity.security.v1`` is
    the only check: the kernel holds no password policy of its own (ADR-0012
    decision 6), so the prompt states no length either.
    """
    if mode == "invite":
        return None
    if mode == "stdin":
        password = sys.stdin.readline().rstrip("\r\n")
        problem = await _call(service, "password_problem", email, password)
        if problem:
            raise SystemExit(f"error: {problem}")
        return password
    app = app_name(_checkout_root())
    prompt = f"Password for {email} (console administrator of {app}): "
    for _ in range(PROMPT_ATTEMPTS):
        password = ask(prompt)
        problem = await _call(service, "password_problem", email, password)
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
    service: Any, email: str, args: argparse.Namespace, intent: _Intent
) -> Any:
    """The new administrator's id, or ``None`` when another run made it first
    and ``--if-missing`` allows that."""
    try:
        return await _call(
            service,
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
        raced = await _call(service, "account_standing", email)
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


async def create_admin(
    core: Any, args: argparse.Namespace, *, ask: Callable[[str], str]
) -> int:
    """``sandloom admin create``; returns the exit status."""
    email = args.email.strip()
    mode = _mode_unless_missing(args)
    intent = _intent(args, "first administrator")
    if mode == "invite":
        _check_link_target()
    service = _service(core)
    standing = await _call(service, "account_standing", email)
    if _check_existing(email, standing, args):
        return 0
    mode = mode or _mode(args)
    password = await _read_password(service, email, mode, ask)
    if standing is not None:
        await _call(
            service,
            "take_over_platform_admin",
            standing.user_id,
            intent.reason,
            intent.operator,
            _key("take-over"),
        )
        user_id = standing.user_id
        done = f"{email} is now an administrator; its old password no longer works."
    else:
        user_id = await _new_admin(service, email, args, intent)
        if user_id is None:
            return 0
        done = f"Administrator {email} created."
    token = await _call(service, "set_password_token", user_id)
    if not token:
        raise _Refused(f"identity issued no set-password link for {email}")
    if password is None:
        _print_link(email, token, created=done)
        return 0
    try:
        await _call(
            service, "set_password_with_token", token, password, _key("password")
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
        print(f"{_style().green(done)} {email} can sign in at {_sign_in_hint()}")
    return 0


def _sign_in_hint() -> str:
    try:
        return link_for("x").split("/set-password", 1)[0] + "/"
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


async def list_admins(core: Any, args: argparse.Namespace) -> int:
    """``sandloom admin list``; returns the exit status."""
    intent = _listing_intent(args)
    service = _service(core)
    show = intent is None or bool(args.show_emails)
    if intent is not None:
        await _call(
            service,
            "record_admin_listing",
            intent.reason,
            intent.operator,
            bool(args.show_emails),
            _key("list"),
        )
    admins = await _call(service, "list_platform_admins")
    if not admins:
        print(
            "No administrator yet. Create one: uv run sandloom admin create "
            "--email you@example.com"
        )
        return 0
    rows = [
        (
            admin.email if show else mask_email(admin.email),
            ("set" if admin.password_set else "not set")
            + ("" if admin.is_active else " (deactivated)"),
            _utc(admin.last_login_at) if admin.last_login_at else "never",
        )
        for admin in admins
    ]
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
    _check_link_target()
    service = _service(core)
    standing = await _call(service, "account_standing", email)
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
        await _call(
            service,
            "reissue_set_password",
            standing.user_id,
            intent.reason,
            intent.operator,
            _key("invite"),
        )
    except ContractCallError as exc:
        raise _Refused(f"{email}: {exc.detail}") from exc
    token = await _call(service, "set_password_token", standing.user_id)
    if not token:
        raise _Refused(f"identity issued no set-password link for {email}")
    _print_link(email, token, created=f"A new link for {email}; its sessions ended.")
    return 0


def _export(args: argparse.Namespace) -> int:
    from sandloom.core.composition import load_composition_app  # noqa: PLC0415 - cold: static command - only the export builds the app and settings; --help works unconfigured

    try:
        payload = (
            json.dumps(
                compile_app(load_composition_app()),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
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
    except ContractCallError as exc:
        print(f"error: identity refused: {exc.detail}", file=sys.stderr)
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
    "SECURITY_CONTRACT",
    "create_admin",
    "invite_admin",
    "link_for",
    "list_admins",
    "main",
    "mask_email",
]
