"""``sandloom admin`` — the console's operator commands.

    sandloom admin export --output FILE [--check]
        compile the static admin manifest (module declarations + public OpenAPI)
    sandloom admin create --email ADDRESS [--name NAME]
                          [--invite | --password-stdin] [--if-missing] [--take-over]
        create a platform administrator. At a terminal it asks for the
        password twice; ``--invite`` prints a single-use set-password link
        (15 minutes) instead; ``--password-stdin`` reads one line from a pipe.
        ``--if-missing`` exits 0 when the address is already an administrator.
        ``--take-over`` makes an unverified account that holds the address an
        administrator and invalidates its password.
    sandloom admin invite --email ADDRESS
        a new set-password link for an administrator (the older link and every
        session end)

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
import ipaddress
import json
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sandloom.core.admin import compile_app
from sandloom.core.cli._operator import _login_name, add_operator_arguments
from sandloom.core.composition import load_composition_core
from sandloom.core.config.settings import settings
from sandloom.core.module_api._errors import ContractCallError
from sandloom.core.module_api._service_registry import ServiceNotFound

#: The token issuer's operator contract (provided by ``sandloom_identity``).
SECURITY_CONTRACT = "identity.security.v1"
#: Where the link points outside production when FRONTEND_URL is not set.
DEV_BASE_URL = "http://127.0.0.1:8000"
#: ``FRONTEND_URL``'s default: a frontend dev server, not this app's console.
_FRONTEND_DEFAULT = "http://localhost:3000"
#: Files ``sandloom dev run`` keeps for this checkout (``{"port": N}``).
RUN_DIR = Path(".sandloom") / "run"
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


def _unspecified(host: str) -> bool:
    """A listen-on-every-interface address, which a browser cannot open."""
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def _dev_run_base(root: Path) -> str | None:
    """The URL of this checkout's ``sandloom dev run``, from its run files."""
    directory = root / RUN_DIR
    if not directory.is_dir():
        return None
    files = sorted(
        (f for f in directory.iterdir() if f.suffix == ".json" and f.is_file()),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("url"), str) and data["url"].startswith("http"):
            return str(data["url"]).rstrip("/")
        port = data.get("port")
        if isinstance(port, int) and 0 < port < 65536:
            host = data.get("host") if isinstance(data.get("host"), str) else None
            if not host or _unspecified(host):
                host = "127.0.0.1"
            return f"http://{host}:{port}"
    return None


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

    The console page reads the token (and the address it pre-fills) from the
    URL fragment, which no server or proxy log records.
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
            base = _dev_run_base(Path.cwd()) or DEV_BASE_URL
        fragment = f"token={token}" + (f"&email={quote(email)}" if email else "")
        return f"{base}/admin/set-password#{fragment}"
    if frontend is None:
        raise SystemExit(
            "error: the admin console is off (ADMIN_CONSOLE_ENABLED): set "
            "FRONTEND_URL to a frontend that serves /auth/reset-password, or "
            "enable the console"
        )
    # The same page the reset emails open.
    return f"{frontend}/auth/reset-password?token={token}"


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


def _print_link(email: str, token: str, *, created: str) -> None:
    print(
        f"{created} Set the password within 15 minutes:\n"
        f"  {link_for(token, email)}\n"
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
    """The password to set, checked against identity's policy (no invite)."""
    if mode == "invite":
        return None
    if mode == "stdin":
        password = sys.stdin.readline().rstrip("\r\n")
        problem = await _call(service, "password_problem", email, password)
        if problem:
            raise SystemExit(f"error: {problem}")
        return password
    for _ in range(PROMPT_ATTEMPTS):
        password = ask("Password: ")
        problem = await _call(service, "password_problem", email, password)
        if problem:
            print(f"{problem}. Try again.", file=sys.stderr)
            continue
        if ask("Password (again): ") != password:
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


async def create_admin(
    core: Any, args: argparse.Namespace, *, ask: Callable[[str], str]
) -> int:
    """``sandloom admin create``; returns the exit status."""
    email = args.email.strip()
    mode = _mode(args)
    intent = _intent(args, "first administrator")
    if mode == "invite":
        _check_link_target()
    service = _service(core)
    standing = await _call(service, "account_standing", email)
    if _check_existing(email, standing, args):
        return 0
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
        print(f"The password was not set ({exc.detail}).", file=sys.stderr)
        _print_link(email, token, created=done)
        return 1
    print(f"{done} {email} can sign in at {_sign_in_hint()}")
    return 0


def _sign_in_hint() -> str:
    try:
        return link_for("x").split("/set-password", 1)[0] + "/"
    except SystemExit:
        return "the console"


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
    add_operator_arguments(create)

    invite = commands.add_parser(
        "invite", help="a new set-password link for an administrator"
    )
    invite.add_argument("--email", required=True, help="the administrator's address")
    add_operator_arguments(invite)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "export":
        return _export(args)
    try:
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


__all__ = ["SECURITY_CONTRACT", "create_admin", "invite_admin", "link_for", "main"]
