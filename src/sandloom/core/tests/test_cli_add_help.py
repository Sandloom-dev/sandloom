""":program:`sandloom add --help`: every generator and every argument says what it
is, and ``--field`` states the ``name:type`` format with the types it accepts."""

from __future__ import annotations

import argparse

import pytest

from sandloom.core.cli import add


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:  # type: ignore[type-arg]
    found = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(found) == 1, "sandloom add has exactly one set of generators"
    return found[0]


def missing_help(
    parser: argparse.ArgumentParser, where: str = "sandloom add"
) -> list[str]:
    """What ``parser`` leaves undocumented: a subparser listed without a summary
    and an argument (an option or a positional) without ``help=``, recursively."""
    gaps = []
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            summaries = {a.dest for a in action._choices_actions if a.help}
            for name, child in action.choices.items():
                if name not in summaries:
                    gaps.append(f"{where} {name}: no summary in the generator list")
                gaps += missing_help(child, f"{where} {name}")
            continue
        if not action.help:
            gaps.append(f"{where}: {action.dest or action.option_strings}")
    return gaps


def test_every_generator_and_argument_has_help() -> None:
    assert missing_help(add._parser()) == []


def test_the_top_description_is_for_a_user_not_a_spec_reference() -> None:
    text = " ".join((add._parser().description or "").split())
    assert "--root" in text and "sandloom generate" in text
    assert "spec §" not in text and "``" not in text


def test_the_checker_names_a_generator_and_an_argument_without_help() -> None:
    """Meta: the check above bites. A parser with one undocumented generator and
    one undocumented argument names both."""
    parser = argparse.ArgumentParser(prog="probe")
    sub = parser.add_subparsers(dest="what", required=True)
    bare = sub.add_parser("bare")  # no help=: not in the generator list
    sub.add_parser("fine", help="documented").add_argument("module", help="the module")
    bare.add_argument("name")  # no help=
    assert missing_help(parser, "probe") == [
        "probe bare: no summary in the generator list",
        "probe bare: name",
    ]


def test_the_checker_reaches_the_arguments_of_the_real_parser() -> None:
    """The walk is not vacuous: it sees every generator of ``sandloom add``."""
    names = set(_subparsers(add._parser()).choices)
    assert {"module", "event", "contract", "consumer", "saga", "task"} <= names


@pytest.mark.parametrize("generator", ["event", "consumer"])
def test_field_states_its_format_the_types_and_that_it_repeats(generator: str) -> None:
    child = _subparsers(add._parser()).choices[generator]
    field = next(a for a in child._actions if a.option_strings == ["--field"])
    assert field.metavar == "NAME:TYPE"
    assert "repeat" in (field.help or "")
    for kind in add.FIELD_TYPES:
        assert kind in (field.help or "")


def test_every_type_the_help_lists_is_accepted_and_one_it_omits_is_refused() -> None:
    """The help lists ``FIELD_TYPES``; the parser that reads ``--field`` takes
    exactly those."""
    for kind in add.FIELD_TYPES:
        assert add.parse_fields([f"value:{kind}"])[0][:2] == (
            "value",
            add.FIELD_TYPES[kind][0],
        )
    with pytest.raises(add.GeneratorError, match="str"):
        add.parse_fields(["value:decimal128"])


def test_the_event_help_states_the_tenant_default_and_the_single_tenant_exception() -> (
    None
):
    text = _subparsers(add._parser()).choices["event"].format_help()
    assert "tenant_id" in text and "TENANCY_MODE=single" in text
