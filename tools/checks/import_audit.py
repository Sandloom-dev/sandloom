"""Import audit: what importing a module does besides defining names (spec §2.4).

Importing a public ``sandloom.sdk`` module (and the bundled business modules) may
define classes and functions, register ORM mappers and metadata, create
loggers and metric objects and register event schemas. It must not read the
environment or ``.env``, construct ``Settings``, open sockets or files (other
than package resources), install logging handlers, construct engines, clients
or Celery apps, start threads, or install signal / ``atexit`` handlers.

This script installs spies for each of those effects **before** anything of
``sandloom`` is imported, imports the requested modules one by one and reports
every effect whose immediate caller is framework code (a frame under the
``sandloom`` package). Effects of third-party libraries at their own import are
listed separately (``--show-foreign``) and never fail the audit.

Usage (from the repository root)::

    uv run python tools/checks/import_audit.py                 # the default set
    uv run python tools/checks/import_audit.py sandloom.core.di     # specific modules
    uv run python tools/checks/import_audit.py --json          # machine-readable

``test_sdk_import_is_side_effect_free`` runs it in a subprocess with
``python -I``, an empty environment and an empty working directory.
Exit status: 0 clean, 1 at least one violation.
"""

from __future__ import annotations

import argparse
import atexit
import builtins
import importlib
import importlib.util
import json
import logging
import os
import signal
import socket
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: What the audit imports when no module is named: every public sandloom.sdk
#: module and the bundled business modules.
DEFAULT_MODULES: tuple[str, ...] = (
    "sandloom.sdk",
    "sandloom.sdk.module",
    "sandloom.sdk.app",
    "sandloom.sdk.migrations",
    "sandloom.sdk.tooling",
    "sandloom.sdk.web",
    "sandloom.sdk.data",
    "sandloom.sdk.tenant",
    "sandloom.sdk.events",
    "sandloom.sdk.effects",
    "sandloom.sdk.contracts",
    "sandloom.sdk.authz",
    "sandloom.sdk.sagas",
    "sandloom.sdk.projections",
    "sandloom.driver",
    "sandloom.sdk.infra",
    "sandloom.sdk.security",
    "sandloom.sdk.errors",
    "sandloom.sdk.testing",
    "sandloom_identity",
    "sandloom_tenancy",
    "sandloom_billing",
)


@dataclass
class Effect:
    kind: str
    detail: str
    where: str
    importing: str
    stack: list[str] = field(default_factory=list)


_EFFECTS: list[Effect] = []
_FOREIGN: list[Effect] = []
_CURRENT = ["<startup>"]
_GUARD = threading.local()
_SANDLOOM_ROOT: str | None = None
_THIS = str(Path(__file__).resolve())


def _sandloom_root() -> str | None:
    global _SANDLOOM_ROOT
    if _SANDLOOM_ROOT is None:
        spec = importlib.util.find_spec("sandloom")
        if spec is None or not spec.submodule_search_locations:
            return None
        _SANDLOOM_ROOT = str(
            Path(next(iter(spec.submodule_search_locations))).resolve()
        )
    return _SANDLOOM_ROOT


def _in_sandloom(filename: str) -> bool:
    root = _sandloom_root()
    if root is None:
        return False
    try:
        return str(Path(filename).resolve()).startswith(root + os.sep)
    except OSError:
        return False


_PLUMBING = (os.sep + "os.py", os.sep + "posixpath.py", os.sep + "genericpath.py")


def _plumbing(filename: str) -> bool:
    return filename.startswith("<frozen") or filename.endswith(_PLUMBING)


def _record(kind: str, detail: str) -> None:
    if getattr(_GUARD, "active", False):
        return
    _GUARD.active = True
    try:
        frames = [
            f
            for f in traceback.extract_stack()[:-2]
            if str(Path(f.filename).resolve()) != _THIS
        ]
        # The immediate caller, skipping stdlib plumbing between it and the
        # spy (os.getenv -> Mapping.get -> __getitem__).
        callers = [f for f in frames if not _plumbing(f.filename)]
        if not callers:
            return
        caller = callers[-1]
        stack = [f"{f.filename}:{f.lineno} ({f.name})" for f in frames[-12:]]
        effect = Effect(
            kind=kind,
            detail=detail,
            where=f"{caller.filename}:{caller.lineno} ({caller.name})",
            importing=_CURRENT[0],
            stack=stack,
        )
        (_EFFECTS if _in_sandloom(caller.filename) else _FOREIGN).append(effect)
    finally:
        _GUARD.active = False


def _spy(owner: Any, attr: str, kind: str, describe: Callable[..., str]) -> None:
    """Replace ``owner.attr`` with a wrapper that records, then delegates."""
    orig = getattr(owner, attr)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _record(kind, describe(*args, **kwargs))
        return orig(*args, **kwargs)

    setattr(owner, attr, wrapper)


def _spy_environ() -> None:
    environ_cls = type(os.environ)
    _spy(environ_cls, "__getitem__", "environ", lambda _s, key: f"read {key!r}")
    _spy(environ_cls, "__contains__", "environ", lambda _s, key: f"test {key!r}")
    _spy(environ_cls, "__iter__", "environ", lambda _s: "iterate")


def _spy_process() -> None:
    _spy(socket.socket, "__init__", "socket", lambda *_a, **_k: "socket.socket()")
    _spy(socket, "getaddrinfo", "socket", lambda *a, **_k: f"getaddrinfo{a[:2]!r}")
    _spy(
        logging.Logger,
        "addHandler",
        "logging-handler",
        lambda lg, h: f"{lg.name}.addHandler({type(h).__name__})",
    )
    _spy(logging, "basicConfig", "logging-handler", lambda **_k: "basicConfig()")
    _spy(threading.Thread, "start", "thread", lambda t: f"Thread.start({t.name})")
    _spy(
        atexit,
        "register",
        "atexit",
        lambda f, *_a, **_k: f"atexit.register({getattr(f, '__qualname__', f)!r})",
    )
    _spy(signal, "signal", "signal", lambda n, _h: f"signal.signal({n!r})")


def _spy_open() -> None:
    orig_open = builtins.open

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, os.PathLike)):
            path = str(Path(os.fspath(file)).resolve())
            root = _sandloom_root()
            if root is None or not path.startswith(root + os.sep):
                _record("file", f"open({os.fspath(file)!r})")
        return orig_open(file, *args, **kwargs)

    builtins.open = spy_open


def _spy_settings() -> None:
    # Settings construction (and with it every .env read): pydantic-settings
    # is third-party, so hooking its base class records the construction of
    # sandloom's Settings before sandloom itself is imported.
    base = importlib.import_module("pydantic_settings").BaseSettings
    orig_settings_init = base.__init__

    def settings_init(self: Any, *args: Any, **kwargs: Any) -> None:
        cls = type(self)
        if not cls.__module__.startswith("sandloom."):
            orig_settings_init(self, *args, **kwargs)
            return
        _record_settings(f"{cls.__module__}.{cls.__qualname__}()")
        # The environment / .env reads of the construction are part of this
        # one finding, not separate ones.
        _GUARD.active = True
        try:
            orig_settings_init(self, *args, **kwargs)
        finally:
            _GUARD.active = False

    base.__init__ = settings_init


def _spy_clients() -> None:
    """Engines, clients and Celery apps."""
    for module_name, attr, kind in (
        ("sqlalchemy", "create_engine", "engine"),
        ("sqlalchemy.ext.asyncio", "create_async_engine", "engine"),
        ("redis", "Redis.__init__", "client"),
        ("redis.asyncio", "Redis.__init__", "client"),
        ("celery", "Celery.__init__", "client"),
        ("httpx", "Client.__init__", "client"),
        ("httpx", "AsyncClient.__init__", "client"),
    ):
        try:
            owner: Any = importlib.import_module(module_name)
        except ImportError:
            continue
        *path, leaf = attr.split(".")
        for part in path:
            owner = getattr(owner, part)
        label = f"{module_name}.{attr.removesuffix('.__init__')}()"
        _spy(owner, leaf, kind, lambda *_a, _label=label, **_k: _label)


def _install_spies() -> None:
    _spy_environ()
    _spy_process()
    _spy_open()
    _spy_settings()
    _spy_clients()


def _record_settings(detail: str) -> None:
    # Settings is constructed from kernel code, but the frame that calls
    # BaseSettings.__init__ is pydantic's; attribute it to the sandloom frame.
    if getattr(_GUARD, "active", False):
        return
    frames = [
        f
        for f in traceback.extract_stack()[:-2]
        if str(Path(f.filename).resolve()) != _THIS
    ]
    # The frame that touched the lazy settings, not the settings module.
    sandloom_frames = [
        f
        for f in frames
        if _in_sandloom(f.filename)
        and not f.filename.endswith(os.path.join("core", "config", "settings.py"))
    ]
    caller = sandloom_frames[-1] if sandloom_frames else frames[-1]
    _EFFECTS.append(
        Effect(
            kind="settings",
            detail=detail,
            where=f"{caller.filename}:{caller.lineno} ({caller.name})",
            importing=_CURRENT[0],
            stack=[f"{f.filename}:{f.lineno} ({f.name})" for f in frames[-12:]],
        )
    )


def audit(modules: list[str]) -> tuple[list[Effect], list[Effect], list[str]]:
    """Import ``modules`` under the spies; (violations, foreign, errors)."""
    _sandloom_root()  # resolved before the spies, without importing sandloom
    _install_spies()
    errors: list[str] = []
    for name in modules:
        _CURRENT[0] = name
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - SystemExit from settings is a finding
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    _CURRENT[0] = "<done>"
    return list(_EFFECTS), list(_FOREIGN), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "modules", nargs="*", help="modules to import (default: sandloom.sdk + bundled)"
    )
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument(
        "--show-foreign", action="store_true", help="also list third-party effects"
    )
    parser.add_argument(
        "--help-target",
        default="",
        help="also run help(<module>) after the imports (output discarded)",
    )
    args = parser.parse_args(argv)
    modules = args.modules or list(DEFAULT_MODULES)
    effects, foreign, errors = audit(modules)
    if args.help_target and not errors:
        _CURRENT[0] = f"help({args.help_target})"
        stdout = sys.stdout
        try:
            with open(os.devnull, "w") as sink:
                sys.stdout = sink
                help(sys.modules[args.help_target])
        except BaseException as exc:  # noqa: BLE001 - reported, not raised
            errors.append(f"help({args.help_target}): {type(exc).__name__}: {exc}")
        finally:
            sys.stdout = stdout
        effects = list(_EFFECTS)
    if args.json:
        print(
            json.dumps(
                {
                    "violations": [asdict(e) for e in effects],
                    "foreign": [asdict(e) for e in foreign]
                    if args.show_foreign
                    else [],
                    "errors": errors,
                },
                indent=2,
            )
        )
    else:
        for e in effects:
            print(f"[{e.kind}] {e.detail}\n  at {e.where}")
            print(f"  while importing {e.importing}")
        if args.show_foreign:
            for e in foreign:
                print(f"(foreign) [{e.kind}] {e.detail} at {e.where} ({e.importing})")
        for err in errors:
            print(f"error: {err}")
        print(f"{len(effects)} violation(s), {len(errors)} import error(s)")
    return 1 if effects or errors else 0


if __name__ == "__main__":
    sys.exit(main())
