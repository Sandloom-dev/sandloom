"""Ratchet on baselined lint suppressions (``# noqa: <CODE>``).

The ruff families enabled on 2026-10-24 (ASYNC, BLE, S, SIM, RUF, PERF,
PLC0415, C901) were switched on with their existing violations baselined as
per-line ``# noqa`` markers. This test pins how many markers each of those
codes may carry: the count may only go DOWN.

- A count above its baseline fails: new code must satisfy the rule (or the
  addition is argued in review and the baseline raised in the same change).
- A count below its baseline also fails, asking you to lower the number, so
  every paid-down suppression is locked in.
- ``BLE001`` and bandit (``S*``) markers must say why the code is safe to
  fail open / closed there: ``# noqa: BLE001 - <reason>``.
- A kernel (``sandloom.core``) function-local import (``PLC0415``) states its
  category (ADR-0007): ``- optional: <reason>`` (a dependency that may be
  absent or unconfigured), ``- cold: <reason>`` (import cost / import-time
  side effects kept off the app's import: CLI, test harness, a driver SDK).
  Never a hidden cycle break.
- The config-level escape hatches (``ignore`` / ``per-file-ignores`` entries
  of the ratcheted families) are pinned too, so a rule cannot be silently
  switched off for a whole file instead of being fixed.

Dead markers cannot inflate the baseline: RUF100 fails on any ``# noqa``
that suppresses nothing, and PGH004 forbids a blanket ``# noqa``.
See ``docs/contributing/lint-policy.md``.
"""

from __future__ import annotations

import io
import re
import tokenize
import tomllib
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src"

#: Rule families under the ratchet (code prefixes).
RATCHETED_PREFIXES = ("ASYNC", "BLE", "S", "SIM", "RUF", "PERF", "PLC0415", "C901")

#: Maximum number of noqa markers per code. Lower an entry when you pay
#: suppressions down; never raise one without a review argument.
NOQA_BASELINE: dict[str, int] = {
    "ASYNC109": 19,
    "ASYNC240": 2,
    "BLE001": 208,
    "C901": 82,
    "PERF401": 24,
    # 118 (2026-10-28): +1 optional import, testing/ownership.py: `redis` is
    # the `sandloom[redis]` extra and `sandloom test --help` must run in a
    # lean `pip install sandloom` (tests/integration/test_package_distributions.py).
    "PLC0415": 118,
    "RUF001": 4,
    "RUF005": 2,
    "RUF012": 3,
    "S101": 43,
    "S102": 1,
    "S105": 8,
    "S106": 1,
    "S108": 1,
    "S110": 29,
    "S112": 3,
    "S301": 0,
    "S311": 9,
    "S505": 1,
    # +1 (DX M8): `sandloom test` runs the project's own dev tools (pytest, ruff,
    # mypy, lint-imports) with a fixed argv — one helper, one marker.
    # +1: the split boot check (sandloom.core.testing.deploy_profile) runs `sandloom
    # doctor` per service in a fresh interpreter (sys.executable, fixed code).
    "S603": 5,
    "S608": 25,
    "SIM102": 1,
}

_PLC0415_CATEGORY = re.compile(r"^-\s*(optional|cold):\s*\S")

#: Kernel-only rules: ignored everywhere outside ``sandloom.core`` / ``sandloom.shared``
#: (a business module's lazy import is not framework debt; DX plan R2.7).
KERNEL_ONLY_PATTERN = "!src/sandloom/{core,shared}/**"
KERNEL_ONLY_RULES: frozenset[str] = frozenset({"PLC0415"})

#: Config-level ignores of ratcheted rules (pyproject ``[tool.ruff.lint]``).
CONFIG_IGNORES: frozenset[str] = frozenset(
    {"SIM105", "RUF022", "RUF023", "RUF002", "RUF003"}
)
#: Ratcheted rules ignored for ``**/tests/**``.
TEST_IGNORES: frozenset[str] = frozenset(
    {
        "PLC0415",
        "S101", "S105", "S106", "S107", "S108", "S311", "S603", "S607", "S608",
        "ASYNC109", "ASYNC110", "ASYNC2",
        "SIM300", "SIM117",
        "PERF", "C901",
        "RUF012", "RUF043",
    }
)  # fmt: skip

_NOQA = re.compile(
    r"#\s*noqa:\s*(?P<codes>[A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*)(?P<rest>[^#]*)"
)
_NEEDS_REASON = re.compile(r"^(BLE001|S[0-9]+)$")


def _ratcheted(code: str) -> bool:
    return any(
        code.startswith(p) and (p != "S" or code[1:].isdigit())
        for p in RATCHETED_PREFIXES
    )


def _linted_files() -> list[Path]:
    # Mirrors ruff's extend-exclude: generated module migration revisions.
    return [
        p
        for p in sorted(SRC.rglob("*.py"))
        if not ("migrations" in p.parts and "versions" in p.parts)
    ]


def _markers_in(source: str) -> list[tuple[int, list[str], str]]:
    """(line, codes, trailing text) of every noqa COMMENT (never a string)."""
    found: list[tuple[int, list[str], str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        for m in _NOQA.finditer(tok.string):
            codes = [c.strip() for c in m.group("codes").split(",")]
            found.append((tok.start[0], codes, m.group("rest").strip()))
    return found


def _markers() -> list[tuple[str, int, list[str], str]]:
    return [
        (str(path.relative_to(BACKEND)), line, codes, rest)
        for path in _linted_files()
        for line, codes, rest in _markers_in(path.read_text(encoding="utf-8"))
    ]


def test_marker_scanner_reads_comments_only() -> None:
    source = (
        'doc = "# noqa: BLE001"\n'
        "x = 1  # pragma: no cover  # noqa: BLE001, S110 - teardown\n"
        "y = 2  # noqa: C901\n"
    )
    assert _markers_in(source) == [
        (2, ["BLE001", "S110"], "- teardown"),
        (3, ["C901"], ""),
    ]
    assert _ratcheted("SIM102") and _ratcheted("S101") and _ratcheted("PLC0415")
    assert not _ratcheted("SLF001") and not _ratcheted("F401")


def test_noqa_counts_only_go_down() -> None:
    counts: Counter[str] = Counter()
    for _path, _line, codes, _rest in _markers():
        counts.update(c for c in codes if _ratcheted(c))
    problems = []
    for code in sorted(set(counts) | set(NOQA_BASELINE)):
        have, allowed = counts.get(code, 0), NOQA_BASELINE.get(code, 0)
        if have > allowed:
            problems.append(
                f"{code}: {have} markers > baseline {allowed} — satisfy the rule "
                "instead of suppressing it (or argue the exception in review "
                "and raise the baseline in the same change)"
            )
        elif have < allowed:
            problems.append(
                f"{code}: {have} markers < baseline {allowed} — lower "
                f'NOQA_BASELINE["{code}"] to {have} to lock the paydown in'
            )
    assert not problems, "\n".join(problems)


def test_blind_except_and_bandit_suppressions_carry_a_reason() -> None:
    missing = [
        f"{path}:{line}: # noqa: {', '.join(codes)}"
        for path, line, codes, rest in _markers()
        if any(_NEEDS_REASON.match(c) for c in codes) and not re.match(r"^-\s*\S", rest)
    ]
    assert not missing, (
        "say why the code fails open/closed safely there — "
        "`# noqa: BLE001 - <reason>`:\n" + "\n".join(missing)
    )


def test_config_level_ignores_of_ratcheted_rules_are_pinned() -> None:
    lint = tomllib.loads((BACKEND / "pyproject.toml").read_text())["tool"]["ruff"][
        "lint"
    ]
    ignored = {c for c in lint.get("ignore", []) if _ratcheted(c)}
    assert ignored == CONFIG_IGNORES, (
        "a ratcheted rule was (un)ignored project-wide; update CONFIG_IGNORES "
        f"deliberately: {sorted(ignored ^ CONFIG_IGNORES)}"
    )
    per_file = lint.get("per-file-ignores", {})
    for pattern, codes in per_file.items():
        ratcheted = {c for c in codes if _ratcheted(c)}
        expected = {
            "**/tests/**": TEST_IGNORES,
            KERNEL_ONLY_PATTERN: KERNEL_ONLY_RULES,
        }.get(pattern, frozenset())
        assert ratcheted == expected, (
            f"per-file-ignores[{pattern!r}] changed for ratcheted rules: "
            f"{sorted(ratcheted ^ expected)} — fix the code or pin it here"
        )


def _kernel_plc0415_markers() -> list[tuple[str, int, str]]:
    return [
        (path, line, rest)
        for path, line, codes, rest in _markers()
        if "PLC0415" in codes
        and path.startswith("src/sandloom/core/")
        and "/tests/" not in path
    ]


def test_kernel_function_local_imports_state_their_category() -> None:
    markers = _kernel_plc0415_markers()
    missing = [
        f"{path}:{line}: # noqa: PLC0415 {rest}".rstrip()
        for path, line, rest in markers
        if not _PLC0415_CATEGORY.match(rest)
    ]
    assert not missing, (
        "a kernel function-local import must be hoisted, or say why it stays "
        "local — `# noqa: PLC0415 - optional|cold: <reason>` "
        "(ADR-0007; a cycle break is not a reason):\n" + "\n".join(missing)
    )
