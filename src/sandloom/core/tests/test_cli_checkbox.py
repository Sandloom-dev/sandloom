"""The checkbox list behind ``sandloom new`` and ``sandloom add skills --choose``."""

from __future__ import annotations

import io

import pytest

from sandloom.core.cli import checkbox
from sandloom.core.cli.skills import CODING_AGENTS, pick_agents


def test_keys_move_toggle_and_confirm() -> None:
    out = io.StringIO()
    keys = ["down", "toggle", "down", "down", "toggle", "up", "confirm"]
    assert checkbox.select(["a", "b", "c", "d"], [0], keys=keys, out=out) == [0, 1, 3]
    # The list is redrawn in place and the cursor is shown again at the end.
    assert "\x1b[5F\x1b[J" in out.getvalue()
    assert out.getvalue().endswith("\x1b[?25h")


def test_the_cursor_wraps_and_a_toggles_all() -> None:
    keys = ["up", "toggle", "all", "confirm"]
    assert checkbox.select(["a", "b", "c"], keys=keys, out=io.StringIO()) == [0, 1, 2]
    # a unchecks all when all are checked.
    keys = ["all", "toggle", "confirm"]
    assert checkbox.select(["a", "b"], [0, 1], keys=keys, out=io.StringIO()) == [0]


def test_enter_with_nothing_checked_keeps_the_list_open() -> None:
    out = io.StringIO()
    assert checkbox.select(
        ["a", "b"], keys=["confirm", "toggle", "confirm"], out=out
    ) == [0]
    assert "check at least 1" in out.getvalue()


def test_minimum_zero_accepts_an_empty_answer_and_a_custom_hint() -> None:
    out = io.StringIO()
    chosen = checkbox.select(
        ["a", "b"], keys=["confirm"], out=out, minimum=0, hint="  space to add"
    )
    assert chosen == []
    assert "space to add" in out.getvalue()
    assert "check at least" not in out.getvalue()


def test_the_hint_line_uses_the_symbol_table() -> None:
    from sandloom.core.cli import ui

    assert checkbox.hint_line(ui.ASCII) == (
        "  ^/v move - space check - a all - Enter confirm - Esc cancel"
    )
    assert "↑/↓ move ·" in checkbox.hint_line(ui.UNICODE)
    assert checkbox.render(["a"], [True], 0, symbols=ui.UNICODE)[0] == "\u203a [x] a"


@pytest.mark.parametrize("keys", [["cancel"], []])
def test_esc_or_the_end_of_input_cancels(keys: list[str]) -> None:
    with pytest.raises(checkbox.Cancelled):
        checkbox.select(["a"], [0], keys=keys, out=io.StringIO())


def test_render_marks_the_cursor_and_the_checked() -> None:
    assert checkbox.render(["a", "b"], [False, True], 1, note="n") == [
        "  [ ] a",
        "> [x] b",
        "n",
    ]


def test_pick_agents_preselects_and_maps_keys() -> None:
    out = io.StringIO()
    chosen = pick_agents(
        {"codex": "codex"}, ["codex"], keys=["toggle", "confirm"], out=out
    )
    assert chosen == ["claude", "codex"]
    assert "(found: codex)" in out.getvalue()
    assert len([a for a in CODING_AGENTS if a.key in chosen]) == 2
    with pytest.raises(ValueError, match="--agents"):
        pick_agents({}, ["claude"], keys=["cancel"], out=io.StringIO())


def test_checking_an_item_checks_what_it_requires() -> None:
    # 0 identity, 1 tenancy, 2 admin: admin requires identity.
    labels = ["identity", "tenancy", "admin"]
    requires = {2: (0,)}
    keys = ["down", "down", "toggle", "confirm"]
    chosen = checkbox.select(
        labels, keys=keys, out=io.StringIO(), minimum=0, requires=requires
    )
    assert chosen == [0, 2]
    # Unchecking identity then unchecks admin.
    keys = ["toggle", "confirm"]
    chosen = checkbox.select(
        labels, [0, 2], keys=keys, out=io.StringIO(), minimum=0, requires=requires
    )
    assert chosen == []
