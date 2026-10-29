#!/usr/bin/env python3
"""Replay the tutorial Markdown in an empty application directory.

Executes its shell blocks and writes the complete files included by its Python
blocks, in page order. Generated stubs are compared, never overwritten. With no
``--cli``, the first command installs the published release exactly as documented.
``--cli /path/to/sandloom`` tests a source checkout instead. Supply disposable
PostgreSQL/Redis services, as for ``quickstart_check.py``. Nothing is published.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
import tempfile
from pathlib import Path

from quickstart_check import REPO, QuickstartError, Runner

BLOCK = re.compile(r"```(bash|python)([^\n]*)\n(.*?)```", re.DOTALL)
INCLUDE = re.compile(r'^--8<-- "([^":]+)"\s*$')
TITLE = re.compile(r'title="([^"]+)"')


class TutorialRunner(Runner):
    def check_framework(self) -> None:
        if self.args.cli:
            super().check_framework()
        else:
            version = self.python(
                "from importlib.metadata import version; print(version('sandloom'))"
            )
            print(f"framework: published sandloom {version}")

    def write_file(self, title: str, snippet: str) -> None:
        target = (self.cwd / title).resolve()
        if not target.is_relative_to(self.cwd):
            raise QuickstartError(f"tutorial target escapes the app: {title}")
        match = INCLUDE.fullmatch(snippet.strip())
        if not match:
            raise QuickstartError(f"tutorial needs a complete file include: {title}")
        source = (self.repo / match[1]).resolve()
        if not source.is_relative_to(self.repo):
            raise QuickstartError(f"tutorial snippet escapes the repository: {source}")
        content = source.read_text(encoding="utf-8")
        if "/shared/contracts/" in title:
            if target.read_text(encoding="utf-8") != content:
                raise QuickstartError(
                    f"generated stub differs from the tutorial: {title}"
                )
            print(f"verified generated stub: {title}")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        print(f"wrote complete tutorial file: {title}")

    def run(self) -> None:
        work = self.args.workdir or Path(tempfile.mkdtemp(prefix="sandloom-tutorial-"))
        work.mkdir(parents=True, exist_ok=True)
        if any(work.iterdir()):
            raise QuickstartError(f"{work} is not empty")
        self.cwd = work.resolve()
        self.configure_services()
        self.env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = "2"
        tutorial = (self.repo / "docs/getting-started/tutorial.md").read_text()
        try:
            for match in BLOCK.finditer(tutorial):
                language, attributes, body = match.groups()
                if language == "python":
                    title = TITLE.search(attributes)
                    if title is None:
                        raise QuickstartError(
                            "a tutorial Python block has no target title"
                        )
                    self.step(f"edit {title[1]}", self.write_file, title[1], body)
                    continue
                for raw in body.splitlines():
                    line = raw.split("#", 1)[0].strip()
                    if not line:
                        continue
                    if line.startswith("uvx ") and self.args.cli:
                        line = (
                            f"{shlex.quote(str(self.args.cli.resolve()))} new tasklist "
                            f"--no-batteries --module tasks --framework-source path "
                            f"--framework-path {shlex.quote(str(self.repo))}"
                        )
                    if "uvicorn" in line:
                        self.module = "tasks"
                    self.do_line(line)
            self.step(
                "final generated-file check",
                self.sh,
                "uv run sandloom generate --check",
            )
        finally:
            self.stop_server()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--cli", type=Path)
    parser.add_argument(
        "--postgres-url", required=True, help="disposable server, without DB"
    )
    parser.add_argument("--admin-db", default="postgres")
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--redis-db-base", type=int, default=0)
    parser.add_argument("--db-prefix", default="sandloom_tutorial_test")
    parser.add_argument("--serve-timeout", type=float, default=90.0)
    runner = TutorialRunner(parser.parse_args(argv))
    try:
        runner.run()
    except (QuickstartError, OSError) as exc:
        print(f"tutorial: {exc}", file=sys.stderr)
        print(runner.summary(False))
        return 1
    print(runner.summary(True).replace("quickstart", "tutorial"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
