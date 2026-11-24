"""SANDLOOM-C039: a module registers no listener at the cursor.

The cursor tenant check runs inside the dialect's ``do_execute`` family, after
every ``before_cursor_execute`` listener (ADR-0018, decision 8). A listener at
the cursor rewrites the SQL text or the parameters after the check read them,
or makes SQLAlchemy skip the check, so module code is refused it. The rule
catches the code an author or an AI agent writes in good faith, under the
spellings that code uses; a deliberate bypass is stopped at runtime, by the
cursor guard and by row-level security, and the rule says so (it cannot be
waived either: a comment is no review). The cases of
``_fixtures/c039/cases.py.txt`` are one module file each: every ``bad`` one must
be reported, with the message of the ``kind`` it names (a branch removed from the
analyzer fails its own cases), and no ``ok`` one. The runtime half, what the
listeners can do once registered, is
``core/tests/test_tenant_guard_cursor_listener.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sandloom.core.archcheck import load_rules, run_checks
from sandloom.core.archcheck.analyzers import find_cursor_listeners
from sandloom.core.archcheck.runner import RULES
from sandloom.core.error_codes import CATALOGUE

_TEXT = (Path(__file__).parent / "_fixtures" / "c039" / "cases.py.txt").read_text(
    encoding="utf-8"
)
_PARTS = re.split(r"^# === (bad|ok)(?:\[(\w+)\])?: (\w+)\n", _TEXT, flags=re.MULTILINE)
_CASES = [
    (_PARTS[i], _PARTS[i + 1], _PARTS[i + 2], _PARTS[i + 3])
    for i in range(1, len(_PARTS), 4)
]

#: What each kind of case is reported as (a part of the analyzer's message).
_MESSAGE = {
    "string": "names a cursor-level event",
    "connection": "handed the DBAPI connection",
    "uncalled": "used without being called",
    "unreadable": "an event name the check cannot read",
    "module": "passed around or stored",
    "star": "star import from sqlalchemy.event",
    "dynamic": "by name, which hides the listeners",
    "dispatch": "listener list of a cursor-level event",
    "dialect": "dialect method that holds the cursor tenant check",
}
_CURSOR_EVENTS = (
    "before_cursor_execute",
    "after_cursor_execute",
    "do_execute",
    "do_executemany",
    "do_execute_no_params",
    "do_setinputsizes",
)
_CONNECTION_EVENTS = (
    "do_connect",
    "connect",
    "first_connect",
    "checkout",
    "checkin",
    "reset",
    "invalidate",
    "handle_error",
    "after_execute",
    "engine_connect",
)


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("verdict", "kind", "name", "source"),
    _CASES,
    ids=[f"{verdict}-{name}" for verdict, _kind, name, _source in _CASES],
)
def test_the_case_is_judged(
    tmp_path: Path, verdict: str, kind: str, name: str, source: str
) -> None:
    hits = find_cursor_listeners(_write(tmp_path, name, source))
    if verdict == "ok":
        assert hits == []
        return
    assert hits, "not reported"
    assert any(_MESSAGE[kind] in hit for hit in hits), (kind, hits)
    assert all(re.match(r"Line \d+: ", hit) for hit in hits), hits


def test_the_fixture_covers_every_kind_and_every_event() -> None:
    bad = [
        (kind, source) for verdict, kind, _name, source in _CASES if verdict == "bad"
    ]
    assert {kind for kind, _ in bad} == set(_MESSAGE)
    assert {verdict for verdict, *_ in _CASES} == {"bad", "ok"}
    assert len(_CASES) >= 80
    for event_name in _CURSOR_EVENTS:
        assert any(
            kind == "string" and f'"{event_name}"' in source for kind, source in bad
        ), f"no case registers {event_name}"
    for event_name in _CONNECTION_EVENTS:
        assert any(
            kind == "connection" and f'"{event_name}"' in source for kind, source in bad
        ), f"no case registers {event_name}"


def test_a_cursor_event_is_reported_once_at_its_line(tmp_path: Path) -> None:
    source = {name: src for _v, _k, name, src in _CASES}[
        "before_cursor_execute_on_an_engine"
    ]
    hits = find_cursor_listeners(_write(tmp_path, "once", source))
    assert len(hits) == 1 and hits[0].startswith("Line 5: "), hits


def test_a_listener_on_every_other_orm_event_stays_allowed(tmp_path: Path) -> None:
    events = (
        "before_insert",
        "after_insert",
        "before_update",
        "after_update",
        "before_delete",
        "after_delete",
        "before_flush",
        "after_flush",
        "after_commit",
        "load",
        "refresh",
        "init",
        "set",
        "do_orm_execute",
        "before_execute",
        "begin",
    )
    source = "from sqlalchemy import event\n" + "".join(
        f'event.listen(Model, "{name}", fn)\n' for name in events
    )
    assert find_cursor_listeners(_write(tmp_path, "orm", source)) == []


def _app(tmp_path: Path, files: dict[str, str]) -> Path:
    modules = tmp_path / "acme" / "modules"
    (modules / "orders").mkdir(parents=True)
    (modules / "__init__.py").write_text("")
    (modules / "orders" / "__init__.py").write_text("")
    for name, body in files.items():
        target = modules / "orders" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    manifest = tmp_path / "architecture_rules.yml"
    manifest.write_text(
        "modules_path: acme/modules\n"
        "modules_package: acme.modules\n"
        "public_api:\n  - sandloom.sdk\n"
    )
    return manifest


_LISTENER = (
    "from sqlalchemy import event\n\n"
    "def attach(engine):\n"
    '    event.listen(engine, "before_cursor_execute", rewrite, retval=True)\n'
)


def test_cursor_listener_runs_in_sandloom_check_on_every_file_but_tests(
    tmp_path: Path,
) -> None:
    manifest = _app(
        tmp_path,
        {
            "service.py": _LISTENER,
            "api/routes.py": _LISTENER,
            "migrations/versions/0001.py": _LISTENER,
            "ok.py": "from sqlalchemy import event\n"
            'event.listen(Model, "before_insert", fn)\n',
            # a module's own tests count its queries: they are never shipped
            "tests/test_queries.py": _LISTENER,
        },
    )
    violations = run_checks(load_rules(manifest), only=["cursor-listener"])
    assert sorted(v.file.name for v in violations) == [
        "0001.py",
        "routes.py",
        "service.py",
    ]
    assert {v.code for v in violations} == {"SANDLOOM-C039"}
    assert {v.line for v in violations} == {4}


def test_a_waiver_comment_admits_no_cursor_listener(tmp_path: Path) -> None:
    """The rule cannot be waived: a comment is no review, and the listener is
    what the runtime cursor guard exists to stop. The comment is answered, not
    silently ignored."""
    by_rule = _LISTENER.replace(
        "retval=True)",
        "retval=True)  # sandloom: allow cursor-listener reviewed: audit tag",
    )
    by_code = _LISTENER.replace(
        "retval=True)",
        "retval=True)  # sandloom: allow SANDLOOM-C039 reviewed: audit tag",
    )
    other_rule = _LISTENER.replace(
        "retval=True)", "retval=True)  # sandloom: allow raw-sql reviewed: not this"
    )
    manifest = _app(
        tmp_path,
        {
            "by_rule.py": by_rule,
            "by_code.py": by_code,
            "other_rule.py": other_rule,
            "plain.py": _LISTENER,
        },
    )
    violations = run_checks(load_rules(manifest), only=["cursor-listener"])
    notes = {v.file.name: v.message for v in violations}
    assert sorted(notes) == ["by_code.py", "by_rule.py", "other_rule.py", "plain.py"]
    unwaivable = "this rule cannot be waived"
    assert unwaivable in notes["by_rule.py"]
    assert unwaivable in notes["by_code.py"]
    # a comment naming another rule is not this rule's waiver and is not answered
    assert unwaivable not in notes["other_rule.py"]
    assert unwaivable not in notes["plain.py"]


def test_the_other_rules_stay_waivable_and_only_this_one_is_not() -> None:
    from sandloom.core.archcheck.runner import NON_WAIVABLE_RULES

    assert NON_WAIVABLE_RULES == {"cursor-listener"}
    assert NON_WAIVABLE_RULES <= set(RULES)


def _package(tmp_path: Path, files: dict[str, str]) -> Path:
    """``orders/`` next to ``orders/__init__.py`` and the given files."""
    package = tmp_path / "orders"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, body in files.items():
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return package


_HELPER = "from sqlalchemy import event as ev\n"
_USE = 'def attach(engine):\n    ev.listen(engine, "connect", run)\n'


@pytest.mark.parametrize(
    ("helper", "service"),
    [
        (_HELPER, "from .helper import ev\n\n" + _USE),
        (_HELPER, "from orders.helper import ev\n\n" + _USE),
        (_HELPER, "from helper import ev\n\n" + _USE),
        (
            "from sqlalchemy import event\n",
            "from .helper import event as ev\n\n" + _USE,
        ),
        (_HELPER, "from .helper import *\n\n" + _USE),
        (
            _HELPER,
            "import sys\nfrom .helper import ev as other\n\n"
            + _USE.replace("ev.", "other."),
        ),
        (
            "from sqlalchemy.event import listen as register\n",
            "from .helper import register\n\n"
            'def attach(engine):\n    register(engine, "connect", run)\n',
        ),
        (
            "import sqlalchemy as sa\nev = sa.event\n",
            "from .helper import ev\n\n" + _USE,
        ),
    ],
    ids=[
        "relative",
        "absolute-by-package",
        "absolute-by-module",
        "renamed-on-import",
        "star",
        "alias-of-the-imported-name",
        "the-registrar-itself",
        "bound-by-an-assignment",
    ],
)
def test_an_event_alias_imported_from_a_sibling_file_is_followed(
    tmp_path: Path, helper: str, service: str
) -> None:
    package = _package(tmp_path, {"helper.py": helper, "service.py": service})
    hits = find_cursor_listeners(package / "service.py")
    assert hits and any(_MESSAGE["connection"] in hit for hit in hits), hits
    assert find_cursor_listeners(package / "helper.py") == []  # nothing used there


@pytest.mark.parametrize(
    ("helper", "service"),
    [
        # the sibling binds the name to something else
        ("ev = object()\n", "from .helper import ev\n\n" + _USE),
        # the name is not the one the sibling binds
        (_HELPER, "from .helper import other\n\n" + _USE.replace("ev.", "other.")),
        # a listener on an event the rule allows
        (
            _HELPER,
            "from .helper import ev\n\n"
            'def attach(model):\n    ev.listen(model, "before_insert", run)\n',
        ),
        # the module is not the file's own tree: a library with the same name
        (_HELPER, "from helper_not_here import ev\n\n" + _USE),
    ],
    ids=["rebound-to-something-else", "another-name", "allowed-event", "not-in-tree"],
)
def test_a_name_imported_from_a_sibling_file_that_is_not_the_event_module_is_left_alone(
    tmp_path: Path, helper: str, service: str
) -> None:
    package = _package(tmp_path, {"helper.py": helper, "service.py": service})
    assert find_cursor_listeners(package / "service.py") == []


def test_an_alias_from_a_parent_directory_is_followed_by_its_level(
    tmp_path: Path,
) -> None:
    package = _package(
        tmp_path,
        {
            "common.py": _HELPER,
            "api/__init__.py": "",
            "api/routes.py": "from ..common import ev\n\n" + _USE,
            "api/onelevel.py": "from .common import ev\n\n" + _USE,
        },
    )
    assert find_cursor_listeners(package / "api" / "routes.py")
    assert find_cursor_listeners(package / "api" / "onelevel.py") == []


def test_a_sibling_that_cannot_be_read_does_not_stop_the_scan(tmp_path: Path) -> None:
    far = "from " + "." * 80 + "helper import ev\n"
    package = _package(
        tmp_path,
        {
            "broken.py": "def (:\n",
            "service.py": "from .broken import ev\nfrom .missing import ev as e2\n"
            + far
            + _USE
            + 'event_name = "do_execute"\n',
        },
    )
    hits = find_cursor_listeners(package / "service.py")
    # the string is still reported: the unreadable sibling is skipped, not fatal
    assert hits and all("names a cursor-level event" in hit for hit in hits), hits


def test_sandloom_check_follows_the_alias_of_a_sibling_file(tmp_path: Path) -> None:
    manifest = _app(
        tmp_path,
        {
            "helper.py": _HELPER,
            "service.py": "from .helper import ev\n\n" + _USE,
        },
    )
    violations = run_checks(load_rules(manifest), only=["cursor-listener"])
    assert [v.file.name for v in violations] == ["service.py"]
    assert {v.code for v in violations} == {"SANDLOOM-C039"}


def test_the_rule_is_in_the_default_run_and_the_catalogue() -> None:
    assert "cursor-listener" in RULES
    entry = next(e for e in CATALOGUE if e.code == "SANDLOOM-C039")
    assert entry.slug == "cursor-listener" and entry.kind == "check"
