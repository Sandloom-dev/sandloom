"""``sandloom.core.url_split.redact``: the url of a message, its secrets left out.

A password stands in the userinfo or, for a driver that reads it there, in the
query (``?password=``): both are masked, whichever of the two a url carries.
"""

from __future__ import annotations

import pytest

from sandloom.core.url_split import redact

_SECRET = "S3CRETpw"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            f"redis://localhost:6379?password={_SECRET}",
            "redis://localhost:6379?password=***",
        ),
        (
            f"redis://u:{_SECRET}@h:6379/0",
            "redis://u:<password>@h:6379/0",
        ),
        (
            f"redis://u:{_SECRET}@h:6379/0?password={_SECRET}&db=1",
            "redis://u:<password>@h:6379/0?password=***&db=1",
        ),
        (
            f"postgresql://h/x?sslpassword={_SECRET}&sslmode=require",
            "postgresql://h/x?sslpassword=***&sslmode=require",
        ),
    ],
)
def test_the_password_goes_from_the_userinfo_and_from_the_query(
    url: str, expected: str
) -> None:
    assert redact(url) == expected
    assert _SECRET not in redact(url)


def test_a_url_without_a_secret_is_returned_as_it_is() -> None:
    assert redact("redis://127.0.0.1:6379/2") == "redis://127.0.0.1:6379/2"


def test_a_url_that_cannot_be_parsed_is_not_echoed() -> None:
    hostile = f"redis://user:{_SECRET}@host{chr(0xFF03)}x:6379/0"
    assert redact(hostile) == "<unparseable url>"
