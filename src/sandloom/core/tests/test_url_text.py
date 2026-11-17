"""``sandloom.core.cli.url_text``: urls inside a text, masked or dropped.

One module serves ``sandloom config`` / ``doctor`` (mask, keep the host) and the
``dev up`` failure line (drop the url), and the dev CLI reads no settings.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from sandloom.core.cli import config as cli_config
from sandloom.core.cli import doctor as cli_doctor
from sandloom.core.cli import url_text

#: (text, the secret in it, the text with the credentials masked).
_LEAKS = [
    (
        "redis://app:s3cretpw@cache.internal:6379/0",
        "s3cretpw",
        "redis://app:***@cache.internal:6379/0",
    ),
    ("postgresql://u:sec/ret@db/x", "sec/ret", "postgresql://u:***@db/x"),
    ("postgresql://u:pa ss@db/x", "pa ss", "postgresql://u:***@db/x"),
    ("redis://user:p@ss@host:6379/0", "p@ss", "redis://user:***@host:6379/0"),
    ("redis://:pw@h/0", "pw@", "redis://***@h/0"),
    ("postgresql://db/x?password=hunter2", "hunter2", "postgresql://db/x?password=***"),
    (
        "postgresql://db/x?user=u&password=hunter2",
        "hunter2",
        "postgresql://db/x?user=u&password=***",
    ),
    (
        "postgresql://db/x?sslmode=require&token=tok3n&x=1",
        "tok3n",
        "postgresql://db/x?sslmode=require&token=***&x=1",
    ),
    (
        "postgresql+asyncpg://u:p%40ss@db/x",
        "p%40ss",
        "postgresql+asyncpg://u:***@db/x",
    ),
    ("rediss://:s3cretpw@h:6380/0", "s3cretpw", "rediss://***@h:6380/0"),
    (
        "redis://u:first@h1/0 or postgresql://v:second@h2/1",
        "first",
        "redis://u:***@h1/0 or postgresql://v:***@h2/1",
    ),
    (
        "redis://u:first@h1/0 or postgresql://v:second@h2/1",
        "second",
        "redis://u:***@h1/0 or postgresql://v:***@h2/1",
    ),
]


@pytest.mark.parametrize(("text", "secret", "masked"), _LEAKS)
def test_mask_keeps_the_host_and_hides_the_secret(
    text: str, secret: str, masked: str
) -> None:
    assert url_text.mask_url_credentials(text) == masked
    assert secret not in url_text.mask_url_credentials(f"failed: {text} (retrying)")


@pytest.mark.parametrize(("text", "secret", "masked"), _LEAKS)
def test_the_config_and_doctor_maskers_are_this_one(
    text: str, secret: str, masked: str
) -> None:
    """``cli.config`` re-exports it, and ``doctor`` masks through it: a leak
    case fixed here is fixed in ``sandloom config`` and ``sandloom doctor``."""
    assert cli_config.mask_url_credentials is url_text.mask_url_credentials
    assert cli_doctor.mask_url_credentials is url_text.mask_url_credentials
    assert cli_config.redact_value("service_endpoint", text) == masked


@pytest.mark.parametrize(("text", "secret", "masked"), _LEAKS)
def test_drop_removes_the_whole_url(text: str, secret: str, masked: str) -> None:
    dropped = url_text.drop_urls(f"cannot reach {text} now")
    assert secret not in dropped
    assert "://" not in dropped and "@" not in dropped
    assert dropped.startswith("cannot reach <url>")


def test_drop_leaves_a_text_without_a_url_alone() -> None:
    text = "DATABASE_URL is not a valid url (got 'abc'); user@example is not one"
    assert url_text.drop_urls(text) == text
    assert url_text.mask_url_credentials(text) == text


def test_mask_leaves_a_url_without_credentials_alone() -> None:
    text = "see https://docs.example.com/page?x=1 and redis://h:6379/0"
    assert url_text.mask_url_credentials(text) == text


def test_the_module_imports_no_settings() -> None:
    """The dev CLI reads no settings; so does this module."""
    code = (
        "import sys\n"
        "import sandloom.core.cli.url_text\n"
        "bad = [m for m in ('pydantic_settings', 'sandloom.core.config.settings')"
        " if m in sys.modules]\n"
        "sys.exit(1 if bad else 0)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], check=False)
    assert done.returncode == 0
