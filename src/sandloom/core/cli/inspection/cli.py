"""Small noninteractive CLI adapter over the cold inspection interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Literal, NoReturn

from sandloom.core.cli.inspection import context, files, graph, queries, sequences
from sandloom.core.cli.inspection.model import (
    INDEX,
    LIMIT,
    RECEIPT,
    InspectionError,
    encode,
    response,
    sha,
)
from sandloom.core.cli.inspection.read import Reader


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
        else ("json", "mermaid", "html"),
        default="markdown" if command == "context" else "json",
    )
    parser.add_argument(
        "--detail",
        choices=("compact", "full"),
        default="compact",
        help="full adds provenance without changing business scope",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=LIMIT,
        help=f"complete UTF-8 byte budget (default {LIMIT})"
        + ("; JSON graphs only" if command == "graph" else ""),
    )
    parser.add_argument(
        "--require-verified",
        action="store_true",
        help="exit 3 unless selected source basis is verified",
    )
    parser.add_argument(
        "--output",
        help=(
            "contained project-relative artifact destination; stdout receives "
            "JSON acknowledgement"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace a recognized Sandloom export, never an arbitrary existing file",
    )
    if command == "context":
        parser.add_argument(
            "module", help="logical name, import path or stable module:name ID"
        )
        parser.add_argument(
            "--focus", help="stable entity/process ID related to the selected module"
        )
        parser.add_argument(
            "--file",
            action="append",
            default=[],
            help="mandatory module-relative file; repeatable",
        )
    else:
        parser.add_argument(
            "--view",
            choices=("graph", "modules", "sequence", "state", "impact", "diff"),
            default="graph",
        )
        parser.add_argument(
            "--module", help="focus by logical name, import path or stable ID"
        )
        parser.add_argument(
            "--process", help="saga or route stable ID; sequence/state view"
        )
        parser.add_argument("--item", help="stable entity ID; impact view")
        parser.add_argument(
            "--before", help="project-relative historical inspection index; diff view"
        )
        parser.add_argument(
            "--direction", choices=("both", "incoming", "outgoing"), default="both"
        )
        parser.add_argument(
            "--hops", type=int, help="module-neighbor distance; default 1 with a focus"
        )
    return parser


def _arguments(command: str, argv: list[str]) -> argparse.Namespace:
    args = parser_for(command).parse_args(argv)
    if args.max_bytes <= 0:
        raise InspectionError("usage", "--max-bytes must be positive", code=2)
    if command == "graph":
        if (
            (args.process and args.view not in {"sequence", "state"})
            or (args.item and args.view != "impact")
            or (args.before and args.view != "diff")
        ):
            raise InspectionError(
                "usage",
                (
                    "--process needs sequence/state; --item needs impact; --before "
                    "needs diff"
                ),
                code=2,
            )
        if args.view in {"impact", "diff"} and not getattr(
            args, "item" if args.view == "impact" else "before"
        ):
            raise InspectionError(
                "usage", "Impact needs --item; diff needs --before", code=2
            )
        if args.view not in {"graph", "modules"} and (
            args.hops is not None or args.direction != "both"
        ):
            raise InspectionError(
                "usage", "--hops and --direction scope topology views only", code=2
            )
        if args.view in {"impact", "diff"} and args.format == "mermaid":
            raise InspectionError("usage", "Impact/diff support JSON or HTML", code=2)
        if args.module and args.view in {"impact", "diff"}:
            raise InspectionError(
                "usage", "Impact/diff have their own explicit scope", code=2
            )
        if args.hops is not None and (not args.module or args.hops < 0):
            raise InspectionError(
                "usage", "--hops needs --module and a non-negative distance", code=2
            )
        if args.format != "json" and (
            args.view == "modules"
            or any(a.split("=")[0] in {"--max-bytes", "--detail"} for a in argv)
        ):
            raise InspectionError(
                "usage",
                "Discovery, --detail and --max-bytes require graph JSON",
                code=2,
            )
    return args


def _json_requested(command: str, argv: list[str]) -> bool:
    if "--output" in argv or any(a.startswith("--output=") for a in argv):
        return True
    formats = [a.split("=", 1)[1] for a in argv if a.startswith("--format=")]
    formats += [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--format"]
    return (formats[-1] == "json") if formats else command == "graph"


def _export(command: str, args: argparse.Namespace, reader: Reader) -> str:
    if command == "context":
        envelope, text = context.build(
            reader,
            args.module,
            requested=args.file,
            budget=args.max_bytes,
            fmt=args.format,
            detail=args.detail,
            require=args.require_verified,
            focus_selector=args.focus,
        )
    else:
        if args.view in {"sequence", "state"}:
            envelope = sequences.build(
                reader,
                selector=args.process,
                module=args.module,
                require=args.require_verified,
                state=args.view == "state",
            )
        elif args.view == "impact":
            envelope = queries.impact(reader, args.item, args.require_verified)
        elif args.view == "diff":
            envelope = queries.compare(reader, args.before, args.require_verified)
        else:
            envelope = graph.build(
                reader,
                selector=args.module,
                hops=args.hops if args.hops is not None else 1,
                discovery=args.view == "modules",
                detail=args.detail,
                require=args.require_verified,
                direction=args.direction,
            )
        text = encode(envelope)
        if args.format == "json" and len(text.encode()) > args.max_bytes:
            raise InspectionError(
                "budget_exceeded",
                f"Graph needs {len(text.encode())} bytes; "
                "focus with --module/--hops or raise --max-bytes",
            )
        if args.format == "mermaid":
            text = (
                sequences.mermaid(envelope)
                if args.view in {"sequence", "state"}
                else graph.mermaid(envelope)
            )
        elif args.format == "html":
            text = graph.viewer(envelope, reader)
    if args.output:
        if Path(args.output).suffix.lower() in {
            ".py",
            ".toml",
            ".yaml",
            ".yml",
            ".lock",
            ".svg",
            ".png",
            ".ico",
        }:
            raise InspectionError(
                "unsafe_output", "Output cannot replace source/configuration assets"
            )
        protected = [
            INDEX,
            RECEIPT,
            "architecture_rules.yml",
            "pyproject.toml",
            "uv.lock",
            "AGENTS.md",
            "CLAUDE.md",
            ".sandloomignore",
            "README.md",
            "README.rst",
            "LICENSE",
            "NOTICE",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            ".gitignore",
            ".gitattributes",
            ".gitmodules",
            reader.layout["source_root"],
            *[m["source"] for m in reader.modules if m["source"]],
        ]
        # Flat apps can write artifacts beside their source packages.
        protected = [p for p in protected if p != "."]
        protected += [p for m in reader.modules for p in reader.rules(m)]
        protected += list(reader.doc["wire_documents"])
        files.export(
            reader.root, args.output, text, force=args.force, protected=protected
        )
        return encode(
            response(
                command,
                "artifact",
                {
                    "path": args.output,
                    "format": args.format,
                    "bytes": len(text.encode()),
                    "sha256": sha(text.encode()),
                },
                selection=envelope["selection"],
                freshness={"state": envelope["freshness"]["state"]},
            )
        )
    return text


def main(command: Literal["context", "graph"], argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _arguments(command, argv)
        text = _export(command, args, Reader(args.root))
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
