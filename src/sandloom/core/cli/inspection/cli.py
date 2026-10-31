"""Small noninteractive CLI adapter over the cold inspection interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Literal, NoReturn

from sandloom.core.cli.inspection import (
    context,
    graph,
    queries,
    sequences,
    terminal,
)
from sandloom.core.cli.inspection.model import (
    LIMIT,
    Doc,
    InspectionError,
    encode,
    response,
)
from sandloom.core.cli.inspection.read import Reader

ACTIONS = {"impact", "diff", "sequence", "state"}


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise InspectionError("usage", message, code=2)


def parser_for(command: str) -> Parser:
    parser = Parser(
        prog=f"sandloom {command}",
        description=(
            "Offline declared architecture inspection; never imports the "
            "application or regenerates files"
        ),
        epilog=(
            "Actions: graph [MODULE], impact ITEM, diff REV_OR_FILE, "
            "sequence [ID], state ID. Use module:NAME for reserved module names. "
            "Text by default; JSON requires --format json."
        )
        if command == "graph"
        else None,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="project root containing architecture_rules.yml",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json")
        if command == "context"
        else ("text", "json", "mermaid", "html"),
        default="markdown" if command == "context" else "text",
    )
    parser.add_argument(
        "--require-verified",
        action="store_true",
        help="exit 3 unless selected source basis is verified",
    )
    if command == "context":
        parser.add_argument(
            "module", help="logical name, import path or stable module:name ID"
        )
    else:
        parser.add_argument("selectors", nargs="*", metavar="ACTION_OR_MODULE")
        parser.add_argument(
            "--detail",
            choices=("compact", "full"),
            default="compact",
            help="full JSON adds provenance and verification evidence",
        )
        parser.add_argument(
            "--max-bytes",
            type=int,
            default=LIMIT,
            help=f"complete JSON byte budget (default {LIMIT})",
        )
        parser.add_argument("--direction", choices=("both", "incoming", "outgoing"))
        parser.add_argument(
            "--hops", type=int, help="module-neighbor distance; default 1 with a focus"
        )
    return parser


def _arguments(command: str, argv: list[str]) -> argparse.Namespace:
    parser = parser_for(command)
    if command == "context":
        return parser.parse_args(argv)
    args = parser.parse_intermixed_args(argv)
    selectors = args.selectors
    args.action = selectors[0] if selectors and selectors[0] in ACTIONS else "graph"
    operands = selectors[1:] if args.action != "graph" else selectors
    required = args.action in {"impact", "diff", "state"}
    if len(operands) > 1 or (required and not operands):
        usage = {
            "graph": "[MODULE]",
            "impact": "ITEM",
            "diff": "REV_OR_FILE",
            "sequence": "[ID]",
            "state": "ID",
        }[args.action]
        action = "" if args.action == "graph" else args.action + " "
        raise InspectionError(
            "usage", f"Use sandloom graph {action}{usage}".rstrip(), code=2
        )
    args.target = operands[0] if operands else None
    if args.max_bytes <= 0:
        raise InspectionError("usage", "--max-bytes must be positive", code=2)
    if (args.hops is not None or args.direction is not None) and args.action != "graph":
        raise InspectionError(
            "usage", "--hops and --direction apply to graph [MODULE] only", code=2
        )
    if args.hops is not None and (not args.target or args.hops < 0):
        raise InspectionError(
            "usage", "--hops needs a module and a non-negative distance", code=2
        )
    if args.action in {"impact", "diff"} and args.format == "mermaid":
        raise InspectionError("usage", "Impact/diff support text, JSON or HTML", code=2)
    if args.format != "json" and any(
        a.split("=")[0] in {"--max-bytes", "--detail"} for a in argv
    ):
        raise InspectionError(
            "usage", "--detail and --max-bytes require graph JSON", code=2
        )
    args.direction = args.direction or "both"
    return args


def _json_requested(command: str, argv: list[str]) -> bool:
    fmt = None
    for i, arg in enumerate(argv):
        if arg == "--":
            break
        if arg.startswith("--format="):
            fmt = arg.split("=", 1)[1]
        elif arg == "--format" and i + 1 < len(argv):
            fmt = argv[i + 1]
    return fmt == "json"


def _graph_text(envelope: Doc, args: argparse.Namespace, reader: Reader) -> str:
    if args.format == "text":
        return terminal.render(
            envelope, overview=args.action == "graph" and args.target is None
        )
    if args.format == "mermaid":
        return (
            sequences.mermaid(envelope)
            if args.action in {"sequence", "state"}
            else graph.mermaid(envelope)
        )
    if args.format == "html":
        return graph.viewer(envelope, reader)
    return encode(envelope)


def _baseline(reader: Reader, target: str, require: bool) -> Doc:
    path = Path(target)
    if (
        (reader.root / path).is_file()
        or path.suffix.lower() == ".json"
        or target.startswith((".", "/"))
    ):
        return queries.compare(reader, target, require)
    return queries.compare_revision(reader, target, require)


def _render(command: str, args: argparse.Namespace, reader: Reader) -> str:
    if command == "context":
        return context.build(
            reader,
            args.module,
            fmt=args.format,
            require=args.require_verified,
        )[1]
    if args.action in {"sequence", "state"}:
        envelope = sequences.build(
            reader,
            selector=args.target,
            module=None,
            require=args.require_verified,
            state=args.action == "state",
            full=args.detail == "full",
        )
    elif args.action == "impact":
        envelope = queries.impact(reader, args.target, args.require_verified)
    elif args.action == "diff":
        envelope = _baseline(reader, args.target, args.require_verified)
    else:
        try:
            envelope = graph.build(
                reader,
                selector=args.target,
                hops=args.hops if args.hops is not None else 1,
                discovery=False,
                detail=args.detail,
                require=args.require_verified,
                direction=args.direction,
            )
        except InspectionError as exc:
            if exc.kind not in {"unknown_module", "ambiguous_module"}:
                raise
            raise InspectionError(
                "usage", str(exc), code=2, actions=exc.actions
            ) from exc
    text = _graph_text(envelope, args, reader)
    if args.format == "json" and len(text.encode()) > args.max_bytes:
        raise InspectionError(
            "budget_exceeded",
            f"Graph needs {len(text.encode())} bytes; "
            "select a module, reduce --hops or raise --max-bytes",
        )
    return text


def main(command: Literal["context", "graph"], argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _arguments(command, argv)
        text = _render(command, args, Reader(args.root))
    except InspectionError as exc:
        if _json_requested(command, argv):
            print(
                encode(
                    response(command, "error", None, diagnostics=[exc.diagnostic()])
                ),
                end="",
            )
        else:
            print(
                f"error[SANDLOOM-T015] {exc.kind}: {exc}\n"
                "https://sandloom.dev/docs/reference/error-codes/#sandloom-t015",
                file=sys.stderr,
            )
        return exc.exit_code
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        SyntaxError,
        ValueError,
    ):
        invalid = InspectionError(
            "invalid_artifact",
            "Cannot safely read inspection inputs; check files and regenerate",
        )
        if _json_requested(command, argv):
            print(
                encode(
                    response(command, "error", None, diagnostics=[invalid.diagnostic()])
                ),
                end="",
            )
        else:
            print(f"error[SANDLOOM-T015]: {invalid}", file=sys.stderr)
        return 1
    print(text, end="")
    return 0
