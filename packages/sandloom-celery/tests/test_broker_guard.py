"""The real-broker guard: an unreachable broker is an error where the lane says
the broker is required (CI), and a skip with its reason only elsewhere."""

from __future__ import annotations

import pytest
from _celery_broker_guard import BROKER_REQUIRED_ENV, broker_required, require_broker

#: Nothing listens on port 1: refused at once. The password must not be echoed.
_REFUSED = "redis://:s3cret-pass@127.0.0.1:1/0"


def test_a_required_broker_that_does_not_answer_fails_the_run() -> None:
    with pytest.raises(pytest.fail.Exception, match=BROKER_REQUIRED_ENV) as caught:
        require_broker(_REFUSED, required=True)

    assert "s3cret" not in str(caught.value)
    assert "127.0.0.1" not in str(caught.value)


def test_a_broker_that_does_not_answer_skips_where_it_is_not_required() -> None:
    with pytest.raises(pytest.skip.Exception, match="does not answer") as caught:
        require_broker(_REFUSED, required=False)

    assert "s3cret" not in str(caught.value)


@pytest.mark.parametrize("required", [True, False])
def test_a_broker_that_answers_passes_either_way(required: bool) -> None:
    require_broker("memory://", required=required)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("0", False), ("", False), ("true", False), (None, False)],
)
def test_only_one_makes_the_broker_required(value: str | None, expected: bool) -> None:
    environ = {} if value is None else {BROKER_REQUIRED_ENV: value}

    assert broker_required(environ) is expected
