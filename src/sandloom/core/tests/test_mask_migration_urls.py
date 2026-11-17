"""The masker of ``sandloom backup plan`` and the migration logs hides every secret.

``_mask_url`` served a plain ``scheme://user:pw@host`` only: a ``?password=``
query value, the second password of a ``;``-separated kombu broker list and the
tail of a password holding a raw ``@`` were printed in full.
"""

from __future__ import annotations

import time

import pytest

from sandloom.core.cli import url_text
from sandloom.core.migrations import url_mask as old_path
from sandloom.core.migrations.backup import _postgres_item, _Server
from sandloom.core.migrations.migrations import _mask_dsns, _mask_url
from sandloom.core.url_mask import (
    MAX_CHAINED_SCHEMES,
    mask_query_secrets,
    mask_url_credentials,
    mask_url_userinfo,
    split_url_list,
)

_CASES = [
    ("redis://u:hunter2@h:6379/0", "hunter2", "redis://***@h:6379/0"),
    ("rediss://:hunter2@h:6380/0", "hunter2", "rediss://***@h:6380/0"),
    # a raw '@' in the password: the userinfo ends at the LAST '@'.
    ("redis://u:pa@ss-hunter2@h:6379/0", "hunter2", "redis://***@h:6379/0"),
    ("redis://u:pa@ss-hunter2@h:6379/0", "pa@ss", "redis://***@h:6379/0"),
    # a password in the query.
    (
        "redis://h:6379/0?password=hunter2&ssl=1",
        "hunter2",
        "redis://h:6379/0?password=***&ssl=1",
    ),
    (
        "postgresql://h/db?user=u&PASSWD=hunter2",
        "hunter2",
        "postgresql://h/db?user=u&PASSWD=***",
    ),
    (
        "redis://h/0?ssl_cert_reqs=none&secret=hunter2",
        "hunter2",
        "redis://h/0?ssl_cert_reqs=none&secret=***",
    ),
    # a kombu broker list: every ';' part is masked, not only the first.
    (
        "redis://u:first@h1:6379/0;redis://u:second-hunter2@h2:6379/0",
        "second-hunter2",
        "redis://***@h1:6379/0;redis://***@h2:6379/0",
    ),
    (
        "redis://u:first@h1:6379/0;redis://u:second@h2:6379/0",
        "first",
        "redis://***@h1:6379/0;redis://***@h2:6379/0",
    ),
    (
        "redis://h1/0;redis://h2/0?password=second-hunter2",
        "second-hunter2",
        "redis://h1/0;redis://h2/0?password=***",
    ),
    # a ';' that starts no url is part of the password or the query, never a split.
    ("postgresql://u:ab;cd@h/db", "ab", "postgresql://***@h/db"),
    ("postgresql://u:ab;cd@h/db", "cd", "postgresql://***@h/db"),
    ("redis://:ab;cd@h/0", "ab", "redis://***@h/0"),
    (
        "redis://u:ab;cd@h1/0;redis://v:ef;gh@h2/0",
        "ef",
        "redis://***@h1/0;redis://***@h2/0",
    ),
    (
        "redis://u:ab;cd@h1/0;redis://v:ef;gh@h2/0",
        "ab",
        "redis://***@h1/0;redis://***@h2/0",
    ),
    ("redis://h/0?a=1;password=hunter2", "hunter2", "redis://h/0?a=1;password=***"),
    ("redis://h/0;password=hunter2", "hunter2", "redis://h/0;password=***"),
    # a query parameter named for what it holds: the name ends in a secret word.
    (
        "postgresql://h/db?sslpassword=hunter2&sslmode=require",
        "hunter2",
        "postgresql://h/db?sslpassword=***&sslmode=require",
    ),
    ("sqs://h/q?access_token=hunter2", "hunter2", "sqs://h/q?access_token=***"),
    ("sqs://h/q?auth_token=hunter2", "hunter2", "sqs://h/q?auth_token=***"),
    ("sqs://h/q?client_secret=hunter2", "hunter2", "sqs://h/q?client_secret=***"),
    ("https://h/c?sv=1&sig=hunter2", "hunter2", "https://h/c?sv=1&sig=***"),
    ("redis://h/0?PASS=hunter2", "hunter2", "redis://h/0?PASS=***"),
    ("redis://h:6379/0", "", "redis://h:6379/0"),
]


@pytest.mark.parametrize(("url", "secret", "masked"), _CASES)
def test_mask_url_hides_every_secret_and_keeps_the_hosts(
    url: str, secret: str, masked: str
) -> None:
    assert _mask_url(url) == masked
    if secret:
        assert secret not in _mask_url(url)


@pytest.mark.parametrize(
    ("url", "secret", "masked"),
    [case for case in _CASES if case[1]],
)
def test_the_backup_plan_prints_no_secret_of_a_backend_server_target(
    url: str, secret: str, masked: str
) -> None:
    # a server the drivers describe (InfraProvider.backup_hint), e.g. Redis
    item = _Server(kind="redis", url=url, roles=["cache:default"]).item()
    assert item.target == masked
    assert secret not in item.target


def test_the_backup_plan_prints_no_secret_of_a_postgres_target() -> None:
    url = "postgresql+asyncpg://u:pa@ss-hunter2@db:5432/app?password=other-hunter2"
    item = _postgres_item("default", url, [])
    rendered = "\n".join([item.target, *item.backup, *item.restore])
    assert "hunter2" not in rendered
    assert "pa@ss" not in rendered
    assert "@db:5432/app" in item.target


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:ab;hunter2@db:5432/app",
        "postgresql+asyncpg://u:ok@db:5432/app?sslpassword=hunter2",
    ],
    ids=["semicolon-password", "sslpassword"],
)
def test_the_backup_plan_prints_no_part_of_a_postgres_secret(url: str) -> None:
    item = _postgres_item("default", url, [])
    rendered = "\n".join([item.target, *item.backup, *item.restore])
    assert "hunter2" not in rendered
    assert "u:ab" not in rendered
    assert "@db:5432/app" in item.target


def test_the_migration_status_error_masks_a_raw_at_and_a_query_password() -> None:
    text = "connect failed: postgresql://u:pa@ss-hunter2@db/x?password=other-hunter2"
    masked = _mask_dsns(text)
    assert "hunter2" not in masked
    assert "@db/x" in masked


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("failed https://sk_live_hunter2:@api.example.com/v1", "hunter2"),
        ("failed https://ghp_hunter2:x-oauth-basic@github.com/o/r.git", "hunter2"),
        ("failed postgresql://role-hunter2:pw@db/x", "hunter2"),
    ],
    ids=["empty-password", "x-oauth-basic", "role"],
)
def test_the_migration_status_error_hides_the_whole_userinfo(
    text: str, secret: str
) -> None:
    """The user slot can hold the secret: ``_mask_dsns`` keeps no part of it."""
    masked = _mask_dsns(text)
    assert secret not in masked
    assert "://***@" in masked


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("https://sk_live_x:@h/", "https://***@h/"),
        ("https://ghp_x:x-oauth-basic@h/", "https://***@h/"),
        ("postgresql://role:pw@h/d", "postgresql://role:***@h/d"),
    ],
    ids=["empty-password", "x-oauth-basic", "role-is-kept"],
)
def test_the_shared_masker_keeps_a_role_but_not_a_token_in_the_user_slot(
    text: str, masked: str
) -> None:
    assert mask_url_credentials(text) == masked


def test_mask_query_secrets_masks_a_value_and_leaves_the_rest() -> None:
    assert mask_query_secrets("x?a=1&token=hunter2&b=2") == "x?a=1&token=***&b=2"
    assert mask_query_secrets("x?a=1&mode=require") == "x?a=1&mode=require"


def test_a_semicolon_in_a_query_value_is_part_of_the_value() -> None:
    """Even a ``;`` that starts ``x://``: a password may hold it, and printing the
    rest as the next url of a list would print the end of the secret."""
    assert mask_query_secrets("redis://h/0?password=a;b") == "redis://h/0?password=***"
    assert (
        mask_query_secrets("redis://h/0?password=ab;cd://x")
        == "redis://h/0?password=***"
    )
    assert (
        mask_query_secrets("redis://h/0?password=a;redis://h2/0")
        == "redis://h/0?password=***"
    )


#: ``;`` + ``x://`` inside a password or a query value: neither part is printed.
_SCHEME_IN_A_SECRET = [
    # the first piece holds ``user:`` + a password that is not a port.
    ("postgresql://u:pw1;kw://pw2@h/db", ("pw1", "pw2"), "postgresql://***@h/db"),
    ("postgresql://u:pw1; kw://pw2@h/db", ("pw1", "pw2"), "postgresql://***@h/db"),
    ("redis://:pw1;kw://pw2@h/0", ("pw1", "pw2"), "redis://***@h/0"),
    (
        "postgresql://u:pw1;kw://pw2;zz://pw3@h/db",
        ("pw1", "pw2", "pw3"),
        "postgresql://***@h/db",
    ),
    # a ``?password=`` value that holds it.
    ("redis://h/0?password=ab;cd://zq", ("ab", "cd", "zq"), "redis://h/0?password=***"),
    (
        "redis://h/0?x=1;password=ab;cd://zq&y=2",
        ("ab", "cd", "zq"),
        "redis://h/0?x=1;password=***&y=2",
    ),
]
#: Lists whose pieces stay apart: a port or an IPv6 host before the ``;``, a
#: ``@`` in each piece.
_LISTS_KEPT_APART = [
    "redis://h1:6379;redis://h2:6379/1",
    "redis://h1:6379/0;redis://h2:6379/1",
    "redis://[::1]:6379;redis://[::2]:6379",
    "amqp://h1;amqp://h2",
]


@pytest.mark.parametrize(("url", "secrets", "masked"), _SCHEME_IN_A_SECRET)
def test_a_semicolon_scheme_inside_a_secret_prints_no_part_of_it(
    url: str, secrets: tuple[str, ...], masked: str
) -> None:
    assert _mask_url(url) == masked
    assert mask_url_credentials(url, keep_user=False) == masked
    for secret in secrets:
        assert secret not in _mask_url(url)
        assert secret not in mask_url_credentials(url)
    assert split_url_list(url) == [url]


@pytest.mark.parametrize("url", _LISTS_KEPT_APART)
def test_a_url_list_with_ports_or_ipv6_hosts_is_split_and_printed_as_is(
    url: str,
) -> None:
    assert len(split_url_list(url)) == 2
    assert _mask_url(url) == url
    assert mask_url_credentials(url) == url


def test_a_list_whose_urls_each_hold_a_password_keeps_every_host() -> None:
    url = "amqp://u:p1@h1;amqp://u:p2@h2/vh"
    assert _mask_url(url) == "amqp://***@h1;amqp://***@h2/vh"
    assert mask_url_credentials(url) == "amqp://u:***@h1;amqp://u:***@h2/vh"
    # a password of digits only before the ';' reads as a host and a port: that
    # is the documented ambiguity of the rule, the url list wins.
    assert split_url_list("postgresql://u:123;b://c@h/db") == [
        "postgresql://u:123",
        "b://c@h/db",
    ]


def test_a_long_word_after_a_scheme_is_masked_in_linear_time() -> None:
    text = "redis://h/" + "a" * 60_000
    started = time.perf_counter()
    assert mask_url_credentials(text) == text
    assert time.perf_counter() - started < 2.0


def _repeat(piece: str, size: int = 50_000) -> str:
    return (piece * (size // len(piece) + 1))[:size]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_repeat("y://u:p;"), id="y-scheme-semicolon"),
        pytest.param(_repeat("a://u:p;"), id="a-scheme-semicolon"),
        pytest.param(_repeat("x://u:p; "), id="semicolon-space"),
    ],
)
def test_a_chain_of_semicolon_schemes_is_not_quadratic(text: str) -> None:
    """Unbounded, every ``y://u:p;`` of a line without ``@`` scanned to the end of
    the line: 12 s on 50 000 characters. The chain is capped."""
    started = time.perf_counter()
    mask_url_credentials(text)
    assert time.perf_counter() - started < 1.0


def test_a_password_that_chains_the_capped_number_of_schemes_is_masked_whole() -> None:
    """The documented limit: eight ``;x://`` pieces are one password. With a ninth
    ``mask_url_credentials`` prints the pieces before the last eight, and
    ``split_url_list`` stops splitting: the rest of the list is one part, masked
    whole (``_mask_url`` prints no secret)."""
    pieces = "".join(f";k{i}://pw{i}" for i in range(MAX_CHAINED_SCHEMES))
    url = f"pg://u:s0{pieces}@h/db"
    assert mask_url_credentials(url, keep_user=False) == "pg://***@h/db"
    assert _mask_url(url) == "pg://***@h/db"
    longer = f"pg://u:s0;z://z{pieces}@h/db"
    assert "s0" in mask_url_credentials(longer, keep_user=False)
    assert split_url_list(longer) == [longer]
    assert _mask_url(longer) == "pg://***@h/db"
    # a list that follows the long chain is part of the rest, never split off
    assert split_url_list(longer + ";redis://h2/0") == [longer + ";redis://h2/0"]


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(_repeat(";sig=a;x://"), id="query-secret-chain"),
        pytest.param(_repeat("?password=;a://"), id="open-query-value-chain"),
        pytest.param(_repeat("x://u:p;"), id="open-password-chain"),
        pytest.param(_repeat("x://u:p")[:-8] + "@;x://a", id="at-after-many-starts"),
        # one piece: every ``?key=`` scanned on to the ``&`` and failed (3 s)
        pytest.param("x://h" + "?key=" * 10_000 + "&;x://h", id="open-secret-then-and"),
        pytest.param("x://h" + "?key=" * 10_000 + " ;x://h", id="open-secret-then-space"),
    ],
)
def test_splitting_a_url_list_is_not_quadratic(url: str) -> None:
    """The open piece was searched again at every ``;x://`` (4 s on 50 000
    characters of ``;sig=a;x://``), and a piece of many ``x://u:p`` before one
    ``@`` was rescanned from every start."""
    started = time.perf_counter()
    split_url_list(url)
    _mask_url(url)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("url", "masked", "masked_with_user"),
    [
        (
            "redis://h1:6379,h2:6379;redis://u:pw@h3",
            "redis://h1:6379,h2:6379;redis://***@h3",
            "redis://h1:6379,h2:6379;redis://u:***@h3",
        ),
        (
            "redis://h1:6379,h2:6379/0;redis://u:pw@h3",
            "redis://h1:6379,h2:6379/0;redis://***@h3",
            "redis://h1:6379,h2:6379/0;redis://u:***@h3",
        ),
    ],
)
def test_a_comma_after_a_port_ends_the_port_and_keeps_the_hosts_of_a_list(
    url: str, masked: str, masked_with_user: str
) -> None:
    """``h1:6379,h2:6379`` is a host list, not ``user:password``: the first two
    hosts stay in the output and the list splits at its ``;``."""
    assert len(split_url_list(url)) == 2
    assert _mask_url(url) == masked
    assert mask_url_credentials(url) == masked_with_user
    assert mask_url_credentials(url, keep_user=False) == masked


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        # sandloom: secret-fixture — synthetic redaction test values
        ("redis://h:6379/0?password=p@ss", "redis://h:6379/0?password=***"),
        (
            "postgresql://u@h/db?sslpassword=ab@cd",
            "postgresql://***@h/db?sslpassword=***",
        ),
        ("redis://u:p@ss@h/0?token=x@y", "redis://***@h/0?token=***"),
    ],
)
def test_a_raw_at_in_a_query_secret_does_not_move_the_end_of_the_userinfo(
    raw: str, masked: str
) -> None:
    """The query is masked first: the userinfo does not run to the ``@`` inside
    ``?password=p@ss`` and eat the name of the parameter."""
    assert _mask_url(raw) == masked
    assert mask_url_credentials(raw, keep_user=False) == masked


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        # sandloom: secret-fixture — synthetic redaction test values
        ("redis://u:p&auth=1@h", "p&auth"),
        ("redis://u:ab;key=xyz@h", "ab"),
        ("redis://u:ab;key=xyz@h", "xyz"),
        ("redis://u:ab&x=1&token=s3@h/0", "ab"),
        ("redis://u:ab&x=1&token=s3@h/0", "s3"),
    ],
)
def test_a_query_name_inside_the_userinfo_is_part_of_the_password(
    raw: str, secret: str
) -> None:
    """``&auth=`` or ``;key=`` in a password is no query: masking it first printed
    the start of the password (``redis://u:p&auth=***``). The userinfo is masked
    whole, in the three maskers."""
    for masked in (
        mask_url_credentials(raw, keep_user=False),
        _mask_url(raw),
        mask_url_userinfo(mask_query_secrets(raw), keep_user=False),
    ):
        assert masked.startswith("redis://***@h"), masked
        assert secret not in masked, masked
    # the keep_user form shows the user and no part of the password
    assert secret not in mask_url_credentials(raw)


def test_a_query_secret_after_the_userinfo_is_still_masked() -> None:
    """The userinfo is skipped only up to its ``@``: the query behind it is not."""
    raw = "redis://u:p&auth=1@h/0?password=zz&x=1"
    assert mask_query_secrets(raw) == "redis://u:p&auth=1@h/0?password=***&x=1"
    assert mask_url_credentials(raw, keep_user=False) == (
        "redis://***@h/0?password=***&x=1"
    )
    # the first ``@`` of a query value does not start a userinfo
    assert mask_query_secrets("redis://h/0?password=a&b=c@d") == (
        "redis://h/0?password=***&b=c@d"
    )


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "passwd",
        "pass",
        "pwd",
        "secret",
        "token",
        "key",
        "sig",
        "signature",
        "auth",
        "credential",
        "credentials",
    ],
)
def test_every_secret_query_name_is_masked(name: str) -> None:
    """Each name of the pattern, alone, behind a prefix and in capitals: removing
    one from ``_QUERY_PARAM_HEAD`` prints its value."""
    for param in (name, f"ssl_{name}", name.upper()):
        raw = f"redis://h/0?x=1&{param}=zzTOPSECRETzz"
        assert mask_query_secrets(raw) == f"redis://h/0?x=1&{param}=***"


def test_the_cli_and_the_migration_runner_mask_with_one_masker() -> None:
    """``cli.url_text`` re-exports the leaf the runner imports: one masker, so a
    leak fixed there is fixed in ``sandloom config``, ``doctor`` and the plan."""
    assert url_text.mask_url_credentials is mask_url_credentials
    assert url_text.mask_query_secrets is mask_query_secrets
    # the old path of the module re-exports the same functions.
    assert old_path.mask_url_credentials is mask_url_credentials
    assert old_path.split_url_list is split_url_list
    assert old_path.mask_url_userinfo is mask_url_userinfo
