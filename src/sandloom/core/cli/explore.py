"""Explicit authoring-and-browser front door; graph itself remains a cold read."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

import sandloom.core.cli.tool_process as _tool_process
from sandloom.core.cli.inspection import files, graph
from sandloom.core.cli.inspection.cli import ACTIONS, _arguments
from sandloom.core.cli.inspection.model import InspectionError
from sandloom.core.cli.inspection.read import Reader

OUTPUT = ".sandloom/cache/explore.html"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandloom explore",
        description="Generate the architecture model, save an offline HTML "
        "explorer and open it in your browser. Generation loads the application "
        "and writes managed artifacts, just like sandloom generate.",
    )
    parser.add_argument("module", nargs="?", help="focus a module and its neighbors")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--hops", type=int)
    parser.add_argument(
        "--direction", choices=("incoming", "outgoing", "both"), default="both"
    )
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="read the existing model without loading the app",
    )
    parser.add_argument(
        "--no-open", action="store_true", help="save HTML without opening a browser"
    )
    args = parser.parse_args(argv)
    graph_argv = [
        "--root",
        str(args.root),
        "--format",
        "html",
        "--direction",
        args.direction,
    ]
    if args.module:
        graph_argv += [
            "module:" + args.module if args.module in ACTIONS else args.module
        ]
    if args.hops is not None:
        graph_argv += ["--hops", str(args.hops)]
    try:
        export_args = _arguments("graph", graph_argv)
        if not args.no_generate:
            code = _generate(args.root)
            if code:
                return code
        reader = Reader(args.root)
        envelope = graph.build(
            reader,
            selector=export_args.target,
            hops=export_args.hops if export_args.hops is not None else 1,
            direction=export_args.direction,
            discovery=False,
            detail="compact",
            require=False,
        )
        files.atomic(reader.root, OUTPUT, graph.viewer(envelope, reader))
        target = (args.root / OUTPUT).resolve()
        uri = target.as_uri()
        print(
            f"Architecture saved · freshness: {envelope['freshness']['state']}\n{uri}"
        )
        if not args.no_open:
            _open(uri)
    except InspectionError as exc:
        print(f"sandloom explore: {exc}", file=sys.stderr)
        return exc.exit_code
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, SyntaxError):
        print(
            "sandloom explore: cannot safely read inspection inputs; "
            "check files and regenerate",
            file=sys.stderr,
        )
        return 1
    return 0


def _generate(root: Path) -> int:
    print("Generating the architecture model…", file=sys.stderr)
    result = _tool_process.run_tool(
        [
            sys.executable,
            "-c",
            "import sandloom; sandloom.main()",
            "generate",
            "--root",
            str(root.resolve()),
        ],
        cwd=root,
        capture=True,
    )
    if result.stdout:
        print(result.stdout, end="", file=sys.stderr)
    if result.returncode:
        print("Generation failed; the explorer was not opened.", file=sys.stderr)
        return max(1, result.returncode)
    return 0


def _open(uri: str) -> None:
    try:
        opened = webbrowser.open(uri, new=2)
    except (webbrowser.Error, OSError):
        opened = False
    if not opened:
        print(
            "No browser available. Open the saved file on your desktop.",
            file=sys.stderr,
        )
