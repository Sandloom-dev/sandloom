#!/usr/bin/env python3
"""Write (default) or check (``--check``) the generator output the docs
include, so the committed files are what a reader generates today:

1. ``docs/snippets/generated/<generator>/``: the ``sandloom new`` + ``sandloom add``
   output the how-to guides include (``docs/snippets/generated/README.md``
   lists the commands; ``SNIPPET_APPS`` below is their one source). Each
   scratch application is generated in a temporary directory and every
   committed snippet file is compared with (or replaced by) its fresh copy.
2. ``examples/tutorial``: the finished tutorial application
   (``docs/getting-started/tutorial.md`` includes its code). Its domain code
   is hand-written; the files ``sandloom generate`` writes in it
   (``module.snapshot.json``, ``routes.snapshot.json``,
   ``contracts.snapshot.json``, contract stubs and DTO copies, exported
   event schemas) are compared with (or written from) the current framework.

Run with the framework's interpreter, from the repository root::

    uv run python tools/docs/docs_generated.py          # write
    uv run python tools/docs/docs_generated.py --check  # CI

``tests/architecture/test_docs_generated.py`` runs ``--check`` in
the backend test jobs. No database or Redis is reached: the tutorial's
composition app is only built (placeholder settings, a fresh random JWT
secret).
"""

from __future__ import annotations

import argparse
import difflib
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SNIPPETS = REPO / "docs" / "snippets" / "generated"
TUTORIAL = REPO / "examples" / "tutorial"
#: The scratch application every snippet directory starts from.
APP = "demo"
#: ``<generator directory>: (the --module of sandloom new, [sandloom add argv, ...])``.
SNIPPET_APPS: dict[str, tuple[str, list[list[str]]]] = {
    "module": ("notes", [["module", "board"]]),
    "event": ("notes", [["event", "notes", "NoteArchived"]]),
    # --module stats: the names match the tutorial app
    "contract": ("stats", [["contract", "stats", "quota"]]),
    "consumer": (
        "notes",
        [
            ["module", "board"],
            ["consumer", "board", "notes.item.created.v1"],
            [
                "consumer",
                "board",
                "billing.invoice.paid.v1",
                "--field",
                "invoice_id:UUID",
                "--field",
                "tenant_id:UUID",
                "--field",
                "amount:int",
            ],
        ],
    ),
    "saga": ("notes", [["saga", "notes", "onboarding"]]),
    "projection": ("notes", [["projection", "notes", "board"]]),
}


def _sandloom(*argv: str) -> None:
    """``sandloom <argv>`` with this interpreter (the framework checkout)."""
    subprocess.run(  # noqa: S603 - fixed argv; the sandloom CLI of this interpreter
        [
            sys.executable,
            "-c",
            "import sys, sandloom; sys.exit(sandloom.main())",
            *argv,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        cwd=REPO,
    )


def _snippet_problems(write: bool) -> list[str]:
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="sandloom-docs-generated-") as tmp:
        scratch = Path(tmp)
        bases: dict[str, Path] = {}
        for generator, (module, commands) in SNIPPET_APPS.items():
            if module not in bases:
                base = scratch / f"base-{module}"
                _sandloom(
                    "new", APP, "--dir", str(base), "--no-batteries", "--module", module
                )
                bases[module] = base / APP
            root = scratch / generator / APP
            shutil.copytree(bases[module], root)
            for command in commands:
                _sandloom("add", "--root", str(root), *command)
        for committed in sorted(SNIPPETS.rglob("*")):
            if not committed.is_file() or committed.name == "README.md":
                continue
            generator, app, *rest = committed.relative_to(SNIPPETS).parts
            # <generator>/<app>/... is the package of that scratch application
            fresh = scratch / generator / app / app / Path(*rest)
            label = committed.relative_to(REPO)
            if not fresh.is_file():
                problems.append(f"{label}: the generators no longer write it")
                continue
            before, after = committed.read_text(), fresh.read_text()
            if before == after:
                continue
            if write:
                committed.write_text(after)
                print(f"wrote {label}")
                continue
            diff = difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                str(label),
                "generated",
                lineterm="",
            )
            problems.append(f"{label}: stale\n" + "\n".join(diff))
    return problems


def _tutorial(write: bool) -> int:
    """The tutorial app's generated files (``sandloom generate`` writes them)."""
    package = tomllib.loads((TUTORIAL / "pyproject.toml").read_text())["project"][
        "name"
    ].replace("-", "_")
    os.chdir(TUTORIAL)
    os.environ.update(
        {
            "ENVIRONMENT": "test",
            "SANDLOOM_COMPOSITION_ROOT": f"{package}.asgi",
            "CONTRACT_DTO_PACKAGES": f'["{package}.shared"]',
            "DATABASE_URL": "postgresql+asyncpg://unused:unused@localhost:5433/unused",
            "CACHE_URL": "redis://localhost:6380/15",
            "BROKER_URL": "redis://localhost:6380/14",
            "JWT_SECRET": secrets.token_hex(32),
        }
    )
    sys.path.insert(0, str(TUTORIAL))
    from sandloom.core.cli.generated import main as generated

    return generated(["--root", str(TUTORIAL), *([] if write else ["--check"])])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="never write (CI)")
    args = parser.parse_args(argv)
    problems = _snippet_problems(write=not args.check)
    for problem in problems:
        print(problem, file=sys.stderr)
    code = _tutorial(write=not args.check)
    if problems or code:
        print(
            "docs generator output is stale: from the repository root, run "
            "`uv run python tools/docs/docs_generated.py` and review the diff",
            file=sys.stderr,
        )
        return 1
    print("docs generator output up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
