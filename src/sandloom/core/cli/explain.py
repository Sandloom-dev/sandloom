"""``sandloom explain`` — the long form of a framework error code.

    sandloom explain SANDLOOM-B002            what happened, why, how to fix it
    sandloom explain b002                (case and the SANDLOOM- prefix are optional)
    sandloom explain tenant-scoped-models  an ``sandloom check`` rule id works too
    sandloom explain --list              every code with its one-line title
    sandloom explain --markdown          the generated reference page

Static: imports no application code and needs no environment.
"""

from __future__ import annotations

import argparse
import sys

from sandloom.core.error_codes import (
    CATALOGUE,
    explain_text,
    lookup,
    render_catalogue_markdown,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom explain", description="Explain a framework error code"
    )
    parser.add_argument("code", nargs="?", help="SANDLOOM-xxxx code or check rule id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="list every code")
    mode.add_argument(
        "--markdown", action="store_true", help="print the reference page"
    )
    args = parser.parse_args(argv)
    if args.list:
        for item in CATALOGUE:
            print(f"{item.code}  {item.slug:34} {item.title}")
        return 0
    if args.markdown:
        sys.stdout.write(render_catalogue_markdown())
        return 0
    if not args.code:
        parser.print_help()
        return 2
    entry = lookup(args.code)
    if entry is None:
        print(
            (
                f"sandloom explain: unknown code {args.code!r} (see "
                f"`sandloom explain --list`)"
            ),
            file=sys.stderr,
        )
        return 2
    print(explain_text(entry))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
