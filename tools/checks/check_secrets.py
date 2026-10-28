#!/usr/bin/env python3
"""Reject credential-shaped material in tracked text without echoing values.

This is a narrow deterministic backstop, not a historical or provider-side
credential audit. Fixtures containing literal keys must opt in on the matching
line with ``sandloom: secret-fixture``; only tests/fixture paths may use that marker.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

RULES = {
    "google-oauth": re.compile(r"GOCSPX-[A-Za-z0-9_-]{20,}"),
    "github-token": re.compile(r"(?:ghp_[A-Za-z0-9]{32,}|github_pat_[A-Za-z0-9_]{40,})"),
    "stripe-live": re.compile(r"(?:sk_live_|rk_live_)[A-Za-z0-9]{20,}"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def findings(path: Path, content: str) -> list[tuple[int, str]]:
    found = []
    fixture = bool(set(path.parts) & {"tests", "_fixtures", "fixtures"})
    lines = content.splitlines()
    for number, line in enumerate(lines, 1):
        previous = lines[number - 2] if number > 1 else ""
        if fixture and "sandloom: secret-fixture" in (previous + line):
            continue
        for name, pattern in RULES.items():
            if pattern.search(line):
                found.append((number, name))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    files = args.files or subprocess.check_output(
        ["git", "ls-files", "-z"], text=True
    ).split("\0")
    failures = 0
    for filename in files:
        path = Path(filename)
        if not filename or not path.is_file() or path.is_symlink():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            continue
        for line, rule in findings(path, content):
            print(f"{path}:{line}: potential {rule} credential (value withheld)")
            failures += 1
    if not failures:
        print("Credential-pattern check: OK")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
