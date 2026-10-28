"""Shared guard rails of the operator CLIs (``sandloom backup`` / ``sandloom
security``).

Every state-changing operator command is:

* **dry-run by default** — it reports what it would do; ``--apply`` acts;
* **refused in production** without ``--confirm-production`` (on top of
  ``--apply``), so a command pasted into the wrong shell does nothing;
* **attributed** — ``--apply`` needs ``--reason``; the operator name comes from
  ``--operator`` (default: the OS login) and lands in the audit record.
"""

from __future__ import annotations

import argparse
import getpass
from dataclasses import dataclass

from sandloom.core.config.settings import settings


@dataclass(frozen=True, slots=True)
class OperatorIntent:
    apply: bool
    operator: str
    reason: str


def add_operator_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--apply", action="store_true", help="act (default: dry-run, report only)"
    )
    parser.add_argument(
        "--confirm-production",
        action="store_true",
        help="required with --apply when ENVIRONMENT=prod",
    )
    parser.add_argument("--reason", default="", help="why (stored in the audit log)")
    parser.add_argument(
        "--operator", default=None, help="who acts (default: the OS login name)"
    )


def operator_intent(args: argparse.Namespace) -> OperatorIntent:
    """Validate the guard-rail flags; ``SystemExit(2)`` with the reason."""
    apply = bool(args.apply)
    operator = (args.operator or _login_name()).strip()
    reason = (args.reason or "").strip()
    if apply:
        if settings.is_prod and not args.confirm_production:
            raise SystemExit(
                "error: refusing to act in production without --confirm-production "
                "(run without --apply first to review the dry-run)"
            )
        if len(reason) < 3:
            raise SystemExit(
                "error: --apply requires --reason (stored in the audit log)"
            )
        if not operator:
            raise SystemExit("error: --apply requires --operator")
    return OperatorIntent(apply=apply, operator=operator, reason=reason)


def _login_name() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name in some containers
        return ""


__all__ = ["OperatorIntent", "add_operator_arguments", "operator_intent"]
