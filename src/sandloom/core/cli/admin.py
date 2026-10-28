"""``sandloom admin export`` — offline module declarations + public OpenAPI → UI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sandloom.core.admin import compile_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandloom admin")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="compile a static admin manifest")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--check", action="store_true", help="fail on manifest drift")
    args = parser.parse_args(argv)
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
