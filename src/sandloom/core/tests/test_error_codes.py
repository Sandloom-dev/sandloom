"""R7: every framework diagnostic has a stable code, a fix and an explain entry."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandloom.core import error_codes
from sandloom.core.archcheck.runner import RULES, Violation
from sandloom.core.cli import check as check_cli
from sandloom.core.cli import explain


def test_every_check_rule_has_a_catalogue_entry() -> None:
    check_slugs = {
        e.slug
        for e in error_codes.CATALOGUE
        if e.kind == "check" and not e.retired and not e.planned
    }
    assert set(RULES) == check_slugs


_SPEC = (
    Path(__file__).resolve().parents[4] / "tests/fixtures/specs/public-api-1.0-spec.md"
)
_APPENDIX_ROW = re.compile(r"^\| (AJA-[BCRT]\d{3}) \| `([a-z0-9-]+)` \|")


def _appendix_a() -> dict[str, str]:
    text = _SPEC.read_text(encoding="utf-8")
    section = text.split("## Appendix A: error codes", 1)[1].split("\n## ", 1)[0]
    rows = (_APPENDIX_ROW.match(line) for line in section.splitlines())
    # The pre-release spec retains its historical brand; the catalogue uses Sandloom.
    return {m.group(1).replace("AJA-", "SANDLOOM-"): m.group(2) for m in rows if m}


def test_every_public_api_10_code_is_catalogued() -> None:
    """Spec Appendix A (Public API 1.0): each code is reserved with its slug."""
    appendix = _appendix_a()
    assert len(appendix) == 31
    for code, slug in appendix.items():
        entry = error_codes.lookup(code)
        assert entry is not None, code
        assert entry.slug == slug, code
        assert not entry.retired, code


def test_planned_codes_name_their_landing_point() -> None:
    # Every Public API 1.0 code has landed (H2b, WS-A B010/B011); a code
    # reserved later must still name its landing point.
    planned = error_codes.planned_codes()
    for entry in planned:
        assert re.match(r"^H[1-4] \(WS-[ABC]\): spec §", entry.planned or ""), (
            entry.code
        )
        assert "Status: planned" in error_codes.explain_text(entry)
        assert entry.slug not in RULES, f"{entry.code}: implemented, clear planned"
    unplanned = [e for e in error_codes.CATALOGUE if not e.planned]
    assert all("Status: planned" not in error_codes.explain_text(e) for e in unplanned)


def test_explain_cli_knows_planned_codes(capsys: pytest.CaptureFixture[str]) -> None:
    planned = [e for e in error_codes.CATALOGUE if e.planned]
    if not planned:
        pytest.skip("every catalogue code has landed")
    entry = planned[0]
    assert explain.main([entry.code]) == 0
    out = capsys.readouterr().out
    assert entry.slug in out
    assert f"lands at {entry.planned}" in out


def test_every_entry_carries_a_fix_and_an_explanation() -> None:
    for entry in error_codes.CATALOGUE:
        assert entry.fix and entry.explain and entry.title, entry.code


def test_lookup_accepts_codes_and_slugs() -> None:
    assert error_codes.lookup("sandloom-b002") is error_codes.lookup("B002")
    assert error_codes.lookup("tenant-scoped-models").code == "SANDLOOM-C014"  # type: ignore[union-attr]
    assert error_codes.lookup("nope") is None


def test_render_uses_the_standard_format() -> None:
    text = error_codes.render(
        "SANDLOOM-B002", "route POST /x resolves the guard first", where="src/x.py:42"
    )
    assert text.splitlines() == [
        "error[SANDLOOM-B002]: route POST /x resolves the guard first",
        "  --> src/x.py:42",
        f"  fix: {error_codes.get('SANDLOOM-B002').fix}",
        "  docs: https://sandloom.dev/docs/reference/error-codes/#sandloom-b002",
        "  help: sandloom explain SANDLOOM-B002",
    ]


def test_every_code_has_a_canonical_web_link_and_offline_help() -> None:
    for entry in error_codes.CATALOGUE:
        url = f"https://sandloom.dev/docs/reference/error-codes/#{entry.code.lower()}"
        assert error_codes.documentation_url(entry.slug) == url
        assert (
            error_codes.documentation_url(entry.code.removeprefix("SANDLOOM-")) == url
        )
        assert url in error_codes.render(entry.code, "probe")
        assert url in error_codes.fix_line(entry.code)
        assert f"Docs: {url}" in error_codes.explain_text(entry)
        assert f"sandloom explain {entry.code}" in error_codes.render(
            entry.code, "probe"
        )


def test_documentation_url_refuses_unknown_codes() -> None:
    with pytest.raises(KeyError):
        error_codes.documentation_url("https://untrusted.example")


def test_a_warning_only_check_report_links_to_its_error_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = SimpleNamespace(
        root=tmp_path, modules_package="notes", business_modules=lambda: ["notes"]
    )
    entry = error_codes.get("C022")
    note = Violation(entry.slug, tmp_path / "notes.py", "probe", severity="warning")
    assert check_cli._report(rules, [note], [], boot_requested=False) == 0
    output = capsys.readouterr().out
    assert error_codes.documentation_url(entry.code) in output
    assert f"sandloom explain {entry.code}" in output


def test_violation_location_is_clickable(tmp_path: Path) -> None:
    v = Violation("uow-boundary", tmp_path / "m" / "s.py", "Line 7: db.commit()")
    assert (
        v.format(tmp_path) == "m/s.py:7: error[SANDLOOM-C006 uow-boundary]: db.commit()"
    )
    plain = Violation("uow-boundary", tmp_path / "m" / "s.py", "whole file")
    assert plain.format(tmp_path).startswith("m/s.py: error[")


def test_exceptions_map_to_codes() -> None:
    class GuardOrderError(RuntimeError):
        pass

    assert error_codes.code_for_exception(GuardOrderError()) == "SANDLOOM-B002"
    assert error_codes.code_for_exception(ValueError()) is None


def test_explain_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert explain.main(["SANDLOOM-C014"]) == 0
    assert "TenantScoped" in capsys.readouterr().out
    assert explain.main(["SANDLOOM-X999"]) == 2
