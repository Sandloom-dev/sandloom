"""Public API surface gate (ADR-0001 versioning).

Snapshots the framework's ENFORCED public surface — the modules a business
module is allowed to import (``public_api`` in ``architecture_rules.yml``) plus
``sandloom.shared`` — and fails the build when a public symbol is **removed**,
**renamed**, has its **kind changed**, or has an **incompatible signature
change** without the committed baseline being updated in the same change.

Adding new public symbols, parameters with defaults, or class members is
backward compatible and never fails the gate; it only records that the baseline
is stale so it can be refreshed intentionally.

Usage (run from the repository root)::

    uv run python tools/packaging/check_public_api.py            # check against baseline
    uv run python tools/packaging/check_public_api.py --update   # regenerate baseline

The baseline lives at ``public_api_baseline.json``.
"""

from __future__ import annotations

import argparse
import enum
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = ROOT / "architecture_rules.yml"
BASELINE_PATH = ROOT / "public_api_baseline.json"

# Extra public packages beyond core's public_api: those a business module may
# import, and sandloom.driverkit, which is for driver packages.
EXTRA_MODULES = (
    "sandloom.driverkit",
    "sandloom.shared",
    "sandloom.shared.authz",
    "sandloom.shared.guards.auth",
    "sandloom.core.testing",
    "sandloom.core.contract_testing",
)

Descriptor = dict[str, Any]
ModuleSnapshot = dict[str, Descriptor]
Snapshot = dict[str, ModuleSnapshot]


def _public_modules() -> list[str]:
    yaml = importlib.import_module("yaml")
    raw = yaml.safe_load(RULES_PATH.read_text())
    data = cast("dict[str, Any]", raw or {})
    modules = list(cast("list[str]", data.get("public_api", []) or []))
    modules.extend(EXTRA_MODULES)
    # Deterministic order, no duplicates.
    return sorted(dict.fromkeys(modules))


def _annotation_str(annotation: object) -> str:
    if annotation is inspect.Parameter.empty:
        return ""
    if isinstance(annotation, str):
        return annotation
    return inspect.formatannotation(annotation)


def _default_str(value: object) -> str:
    """Record meaningful defaults without process-specific sentinel addresses."""
    if value is None or isinstance(value, (str, bool, int, float, bytes)):
        return repr(value)
    if isinstance(value, enum.Enum):
        cls = type(value)
        return f"{cls.__module__}.{cls.__qualname__}.{value.name}"
    if isinstance(value, (tuple, list)):
        return type(value).__name__ + "(" + ",".join(map(_default_str, value)) + ")"
    if isinstance(value, (set, frozenset)):
        return (
            type(value).__name__
            + "("
            + ",".join(sorted(map(_default_str, value)))
            + ")"
        )
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                sorted(f"{_default_str(k)}:{_default_str(v)}" for k, v in value.items())
            )
            + "}"
        )
    cls = type(value)
    return f"<{cls.__module__}.{cls.__qualname__}>"


def _signature_of(obj: object) -> str | None:
    """A deterministic signature string.

    Records parameter names, kinds, annotations and whether a default is
    present, including stable primitive/collection default values and complete
    generic annotations. Opaque sentinels use their type, never an address.
    """
    try:
        sig = inspect.signature(cast("Any", obj))
    except (TypeError, ValueError):
        return None
    parts: list[str] = []
    for param in sig.parameters.values():
        ann = _annotation_str(param.annotation)
        default = (
            "=" + _default_str(param.default)
            if param.default is not inspect.Parameter.empty
            else ""
        )
        piece = f"{param.name}:{param.kind.name}"
        if ann:
            piece += f"|{ann}"
        piece += default
        parts.append(piece)
    ret = _annotation_str(sig.return_annotation)
    rendered = "(" + ", ".join(parts) + ")"
    if ret:
        rendered += f" -> {ret}"
    return rendered


def _public_names(obj: object) -> list[str]:
    explicit = getattr(obj, "__all__", None)
    if isinstance(explicit, (list, tuple)):
        return sorted({str(n) for n in explicit if not str(n).startswith("_")})
    return sorted(n for n in dir(obj) if not n.startswith("_"))


def _describe(obj: object) -> Descriptor:
    if inspect.isclass(obj):
        return {
            "kind": "class",
            "signature": _signature_of(obj),
            "members": sorted(_public_names(obj)),
        }
    if inspect.isfunction(obj) or inspect.isbuiltin(obj) or callable(obj):
        kind = "function" if inspect.isroutine(obj) else "callable"
        return {"kind": kind, "signature": _signature_of(obj)}
    return {"kind": "value"}


def snapshot() -> Snapshot:
    result: Snapshot = {}
    for mod_name in _public_modules():
        module = importlib.import_module(mod_name)
        mod_snap: ModuleSnapshot = {}
        for name in _public_names(module):
            obj = getattr(module, name)
            # Skip re-exported submodules; we snapshot the named modules directly.
            if inspect.ismodule(obj):
                continue
            mod_snap[name] = _describe(obj)
        result[mod_name] = mod_snap
    return result


def _dump(snap: Snapshot) -> str:
    return json.dumps(snap, indent=2, sort_keys=True) + "\n"


def _diff(baseline: Snapshot, current: Snapshot) -> list[str]:
    breaks: list[str] = []
    for mod_name, base_mod in baseline.items():
        cur_mod = current.get(mod_name)
        if cur_mod is None:
            breaks.append(f"module removed: {mod_name}")
            continue
        for name, base_desc in base_mod.items():
            cur_desc = cur_mod.get(name)
            if cur_desc is None:
                breaks.append(f"{mod_name}.{name}: symbol removed")
                continue
            if cur_desc.get("kind") != base_desc.get("kind"):
                breaks.append(
                    f"{mod_name}.{name}: kind changed "
                    f"{base_desc.get('kind')!r} -> {cur_desc.get('kind')!r}"
                )
            if base_desc.get("signature") != cur_desc.get("signature"):
                breaks.append(
                    f"{mod_name}.{name}: signature changed "
                    f"{base_desc.get('signature')!r} -> {cur_desc.get('signature')!r}"
                )
            removed_members = set(base_desc.get("members", [])) - set(
                cur_desc.get("members", [])
            )
            for member in sorted(removed_members):
                breaks.append(f"{mod_name}.{name}.{member}: member removed")
    return breaks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Public API surface gate")
    parser.add_argument(
        "--update", action="store_true", help="regenerate the committed baseline"
    )
    args = parser.parse_args(argv)

    current = snapshot()

    if args.update:
        BASELINE_PATH.write_text(_dump(current), encoding="utf-8")
        print(f"baseline written: {BASELINE_PATH.relative_to(ROOT)}")
        return 0

    if not BASELINE_PATH.exists():
        print(
            "error: no baseline found; run with --update to create "
            f"{BASELINE_PATH.relative_to(ROOT)}",
            file=sys.stderr,
        )
        return 1

    baseline = cast("Snapshot", json.loads(BASELINE_PATH.read_text()))
    breaks = _diff(baseline, current)

    stale = _dump(baseline) != _dump(current)

    if breaks:
        print(
            "Public API gate FAILED — backward-incompatible changes:",
            file=sys.stderr,
        )
        for line in breaks:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\nIf these changes are intentional, bump the affected package's major "
            "version (or minor while 0.x) and refresh the baseline:\n"
            "  uv run python tools/packaging/check_public_api.py --update",
            file=sys.stderr,
        )
        return 1

    if stale:
        print(
            "Public API baseline is stale (only additive changes detected). "
            "Refresh it:\n  uv run python tools/packaging/check_public_api.py --update",
            file=sys.stderr,
        )
        return 1

    print("Public API gate OK — surface matches baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
