"""``sandloom config dump`` — the effective ``Settings``, secrets redacted.

Prints every framework setting the process would run with, after the same
resolution ``create_app`` uses (process env > ``.env`` > ``SECRETS_DIR`` files
> defaults, then the validators), and WHERE each value came from::

    sandloom config dump                 # redacted (default), text
    sandloom config dump --json          # machine-readable
    sandloom config dump --only JWT_,RPC_   # name prefixes
    sandloom config dump --show-secrets  # unredacted; refused when ENVIRONMENT=prod
    sandloom config dump --kernel-only   # the kernel's settings only (no composition)

The default dump also lists the settings the loaded modules declare (ADR-0015),
with each value's source and the same redaction, one block per module. It builds
the composition the way the app does (``SANDLOOM_COMPOSITION_ROOT``) in collect
mode: a module's invalid setting is shown as a diagnosed entry, not an exit. It
only builds: no lifespan, no consumer, relay or task, and no connection (a module's
``register`` / ``setup`` may build a client object, never connect). ``--kernel-only``
skips the composition and prints exactly what the dump printed before modules
declared their own settings.

Redaction (:func:`redact_value`) is by field TYPE (``SecretStr``) and NAME
(``*_SECRET``, ``*PASSWORD*``, ``*_TOKEN``, ``*PRIVATE*``, ``*_KEY``, ``*DSN``
…), recursively inside JSON settings (``INFRA``, ``SERVICE_BINDINGS``), and
every URL's ``user:password@`` is masked wherever it appears. Unset secrets
stay visible as unset — "is it configured?" is the question an operator asks.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from pydantic_settings import (
    DotEnvSettingsSource,
    EnvSettingsSource,
    SecretsSettingsSource,
)

from sandloom.core.composition import load_composition_core
from sandloom.core.config import module_settings as _module_settings
from sandloom.core.config.settings import effective_secrets_dir, settings
from sandloom.core.observability.guardrails import is_secret_name

REDACTED = "<redacted>"

_URL_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)([^/@\s]+)@")


def mask_url_credentials(text: str) -> str:
    """``scheme://user:pw@host`` -> ``scheme://user:***@host`` anywhere in text."""

    def _sub(match: re.Match[str]) -> str:
        userinfo = match.group(2)
        user = userinfo.split(":", 1)[0] if ":" in userinfo else ""
        return f"{match.group(1)}{user + ':' if user else ''}***@"

    return _URL_CREDENTIALS.sub(_sub, text)


def _plain(value: Any) -> Any:
    """JSON-friendly form of a settings value (no secret unwrapping)."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_plain(v) for v in value]
        return sorted(items, key=str) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    secret = getattr(value, "get_secret_value", None)
    if callable(secret):  # SecretStr / SecretBytes
        return REDACTED
    return str(value)


def redact_value(name: str, value: Any, *, secret: bool = False) -> Any:
    """``value`` of setting ``name`` with every credential replaced.

    Everything nested under a credential name is a credential too
    (``RPC_DH_EXTRA_PRIVATE_KEYS={"kid": "<key>"}``); inside other JSON
    settings each key is judged by its own name (``INFRA``'s ``password``).
    """
    secret = secret or is_secret_name(name)
    if callable(getattr(value, "get_secret_value", None)):
        return REDACTED if value.get_secret_value() else value.get_secret_value()
    if isinstance(value, Mapping):
        return {
            str(k): redact_value(str(k), v, secret=secret) for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(name, v, secret=secret) for v in value]
    if isinstance(value, str):
        if secret and value:
            return REDACTED
        return mask_url_credentials(value)
    if secret and isinstance(value, bytes) and value:
        return REDACTED
    return _plain(value)


@dataclass(frozen=True, slots=True)
class ConfigEntry:
    name: str
    value: Any
    source: str
    secret: bool


@dataclass(frozen=True, slots=True)
class ModuleConfigEntry(ConfigEntry):
    """A setting a loaded module declares (``module`` names the owner), or, with
    ``source == "invalid"``, the module's diagnosed configuration problem
    (``value`` is the reason, never a secret's value)."""

    module: str = ""


#: What building the composition fails with that is no module's refusal (a
#: composition root that does not import or expose a core, a failed module
#: load); anything else is a defect and keeps its traceback.
_BUILD_FAILURES = (
    ImportError,
    AttributeError,
    LookupError,
    RuntimeError,
    TypeError,
    ValueError,
    OSError,
)

#: ``ModuleConfigEntry.source`` of a diagnosed problem.
INVALID = "invalid"


def module_config(core: Any, *, redact: bool = True) -> list[ModuleConfigEntry]:
    """Every setting the modules loaded in ``core`` declare, each with the
    origin the process's one source read recorded (``env`` / ``dotenv`` /
    ``secrets_dir``, else ``default``), sorted by module then name. A secret is
    a ``SecretStr`` field or a name the kernel's one secret rule knows."""
    entries: list[ModuleConfigEntry] = []
    records = sorted(core.module_settings.records(), key=lambda r: r.package)
    for record in records:
        for name, field_name in _module_settings.env_names(record.model).items():
            raw = getattr(record.instance, field_name)
            secret = (
                is_secret_name(name) and not isinstance(raw, (bool, int, float))
            ) or callable(getattr(raw, "get_secret_value", None))
            value = redact_value(name, raw) if redact else _plain(raw)
            entries.append(
                ModuleConfigEntry(
                    name=name,
                    value=value,
                    source=record.origins.get(name) or "default",
                    secret=secret,
                    module=record.module,
                )
            )
    return sorted(entries, key=lambda e: (e.module, e.name))


def diagnosed_entries(
    diagnosed: Sequence[_module_settings.Diagnosed],
) -> list[ModuleConfigEntry]:
    return [
        ModuleConfigEntry(
            name=item.module,
            value=mask_url_credentials(item.message),
            source=INVALID,
            secret=False,
            module=item.module,
        )
        for item in diagnosed
    ]


def _secrets_source(settings_cls: type[Any]) -> SecretsSettingsSource:
    """The secrets source ``Settings`` itself uses (``SECRETS_DIR`` included)."""
    return SecretsSettingsSource(
        settings_cls, secrets_dir=effective_secrets_dir(settings_cls)
    )


def _sources(settings_cls: type[Any]) -> list[tuple[str, dict[str, Any]]]:
    """Values each pydantic-settings source supplies, in precedence order."""
    found: list[tuple[str, dict[str, Any]]] = []
    for label, factory in (
        ("env", EnvSettingsSource),
        ("dotenv", DotEnvSettingsSource),
        ("secrets_dir", _secrets_source),
    ):
        try:
            values = factory(settings_cls)()
        except Exception:  # noqa: BLE001 - provenance is best-effort
            values = {}
        found.append((label, {str(k).upper(): v for k, v in values.items()}))
    return found


def effective_config(cfg: Any = None, *, redact: bool = True) -> list[ConfigEntry]:
    """Every setting of ``cfg`` (default: the live settings) with its source.

    ``source`` is the highest-precedence pydantic-settings source that
    supplied the field (``env`` > ``dotenv`` > ``secrets_dir``), else
    ``default``. A value a validator derived (e.g. the strict profile's
    fail-closed defaults) is the effective one; its source stays ``default``.
    """
    if cfg is None:
        cfg = settings
    cls = type(cfg)
    sources = _sources(cls)
    entries: list[ConfigEntry] = []
    for field_name, field in cls.model_fields.items():
        name = str(field.alias or field_name).upper()
        source = next(
            (
                label
                for label, values in sources
                if name in values or field_name.upper() in values
            ),
            "default",
        )
        raw = getattr(cfg, field_name)
        secret = (
            is_secret_name(name) and not isinstance(raw, (bool, int, float))
        ) or callable(getattr(raw, "get_secret_value", None))
        value = redact_value(name, raw) if redact else _plain(raw)
        entries.append(
            ConfigEntry(name=name, value=value, source=source, secret=secret)
        )
    return sorted(entries, key=lambda e: e.name)


def _render_text(entries: Sequence[ConfigEntry]) -> str:
    width = max((len(e.name) for e in entries), default=0)
    lines = []
    for e in entries:
        value = (
            e.value if isinstance(e.value, str) else json.dumps(e.value, default=str)
        )
        lines.append(f"{e.name.ljust(width)} = {value}  [{e.source}]")
    return "\n".join(lines)


def _render_modules(entries: Sequence[ModuleConfigEntry]) -> str:
    """One block per module: ``# module <name>``, its rows, its problems."""
    blocks: list[str] = []
    for module in dict.fromkeys(e.module for e in entries):
        rows = [e for e in entries if e.module == module]
        good = [e for e in rows if e.source != INVALID]
        lines = [f"# module {module}", _render_text(good)] if good else []
        lines += [f"# INVALID - {e.value}" for e in rows if e.source == INVALID]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _build_module_entries(
    redact: bool,
) -> tuple[list[ModuleConfigEntry], str | None]:
    """The modules' settings and diagnosed problems of the composition the
    deployment runs, and a reason when the composition could not be built at
    all (collect mode reports a module's refusal, not another failure)."""
    core = None
    failure: str | None = None
    with _module_settings.collect_mode() as diagnosed:
        try:
            core = load_composition_core()
        except _BUILD_FAILURES as exc:  # reported below, never swallowed
            failure = f"{type(exc).__name__}: {exc}"
        except SystemExit as exc:
            failure = str(exc.code if exc.code is not None else exc)
    entries = module_config(core, redact=redact) if core is not None else []
    entries += diagnosed_entries(diagnosed)
    if diagnosed:
        failure = None  # a diagnosed module explains an incomplete composition
    return entries, mask_url_credentials(failure) if failure else None


def dump_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom config dump",
        description="Print the effective settings (secrets redacted) and their source",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--redacted",
        action="store_true",
        default=True,
        help="redact credentials (the default)",
    )
    mode.add_argument(
        "--show-secrets",
        action="store_true",
        help="print credentials in clear (refused when ENVIRONMENT=prod)",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated name prefixes to include (e.g. JWT_,RPC_)",
    )
    parser.add_argument(
        "--non-default",
        action="store_true",
        help="only settings supplied by env / .env / SECRETS_DIR",
    )
    parser.add_argument(
        "--kernel-only",
        action="store_true",
        help=(
            "only the kernel's settings, without building the composition "
            "(the output of the dump before modules declared their own)"
        ),
    )
    args = parser.parse_args(argv)

    if args.show_secrets and settings.is_prod:
        print(
            "error: refusing --show-secrets with ENVIRONMENT=prod; the redacted "
            "dump shows which secrets are set",
            file=sys.stderr,
        )
        return 2
    entries = effective_config(settings, redact=not args.show_secrets)
    prefixes = [p.strip().upper() for p in args.only.split(",") if p.strip()]
    if prefixes:
        entries = [e for e in entries if e.name.startswith(tuple(prefixes))]
    if args.non_default:
        entries = [e for e in entries if e.source != "default"]
    modules: list[ModuleConfigEntry] = []
    if not args.kernel_only:
        modules, failure = _build_module_entries(redact=not args.show_secrets)
        if failure:
            print(
                f"error: the composition could not be built: {failure}",
                file=sys.stderr,
            )
            return 1
        if prefixes:
            modules = [
                e
                for e in modules
                if e.source == INVALID or e.name.startswith(tuple(prefixes))
            ]
        if args.non_default:
            modules = [e for e in modules if e.source not in ("default",)]
    if args.json:
        print(
            json.dumps([asdict(e) for e in (*entries, *modules)], indent=2, default=str)
        )
    else:
        text = _render_text(entries)
        if modules:
            text += "\n\n" + _render_modules(modules)
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["dump"]:
        return dump_main(args[1:])
    if args[:1] in (["-h"], ["--help"]):
        print(
            "usage: sandloom config dump [--json] [--only PREFIXES] [--non-default] "
            "[--show-secrets] [--kernel-only]\n\nthe effective settings, secrets "
            "redacted, with the source of each value (`sandloom config dump --help`)"
        )
        return 0
    print(
        "usage: sandloom config dump [--json] [--only PREFIXES] [--non-default] "
        "[--show-secrets] [--kernel-only]",
        file=sys.stderr,
    )
    return 2


__all__ = [
    "INVALID",
    "REDACTED",
    "ConfigEntry",
    "ModuleConfigEntry",
    "dump_main",
    "diagnosed_entries",
    "effective_config",
    "is_secret_name",
    "main",
    "mask_url_credentials",
    "module_config",
    "redact_value",
]
