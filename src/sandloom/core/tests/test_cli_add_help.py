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


@pytest.mark.parametrize("generator", ["module", "event", "consumer"])
def test_no_tenant_is_a_flag_of_module_event_and_consumer_with_its_own_help(
    generator: str,
) -> None:
    child = _subparsers(add._parser()).choices[generator]
    flag = next(a for a in child._actions if a.option_strings == ["--no-tenant"])
    assert "without tenants" in (flag.help or "")
    assert "TENANCY_MODE" not in child.format_help()
    assert flag.default is False
    for other in ("contract", "saga", "projection", "task"):
        text = _subparsers(add._parser()).choices[other].format_help()
        assert "--no-tenant" not in text


def test_entity_empty_and_settings_are_flags_of_module_only() -> None:
    choices = _subparsers(add._parser()).choices
    text = " ".join(choices["module"].format_help().split())
    for flag in ("--entity NAME", "--empty", "--settings"):
        assert flag in text
    assert "default: item" in text and "no example entity" in text
    for other in set(choices) - {"module"}:
        other_text = choices[other].format_help()
        assert "--entity" not in other_text and "--empty" not in other_text, other
    parsed = add._parser().parse_args(["module", "shop", "--entity", "order"])
    assert (parsed.entity, parsed.empty, parsed.settings) == ("order", False, False)
    parsed = add._parser().parse_args(["module", "shop", "--empty", "--settings"])
    assert (parsed.entity, parsed.empty, parsed.settings) == (None, True, True)


def test_the_event_help_states_the_tenant_default_and_the_flag_that_drops_it() -> None:
    text = " ".join(_subparsers(add._parser()).choices["event"].format_help().split())
    assert "also gets tenant_id: UUID, unless --no-tenant" in text


def test_every_generator_ends_its_help_with_an_example_that_parses() -> None:
    """One example per generator (``deploy`` has its own parser and help): it is
    in the generator's ``--help`` and the parser accepts it as written."""
    import shlex

    prefix = "uv run sandloom add "
    choices = _subparsers(add._parser()).choices
    assert set(add._EXAMPLES) == set(choices) - {"deploy"}
    for name, example in add._EXAMPLES.items():
        assert example.startswith(prefix), name
        assert f"example: {example}" in " ".join(choices[name].format_help().split())
        parsed = add._parser().parse_args(shlex.split(example.removeprefix(prefix)))
        assert parsed.what == name


def test_an_example_the_parser_refuses_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Meta: the walk above bites. A wrong example (a missing argument) fails."""
    monkeypatch.setitem(add._EXAMPLES, "event", "uv run sandloom add event notes")
    with pytest.raises(SystemExit):
        add._parser().parse_args(["event", "notes"])
