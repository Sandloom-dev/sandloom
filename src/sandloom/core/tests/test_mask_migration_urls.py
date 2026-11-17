"""The masker of ``sandloom backup plan`` and the migration logs hides every secret.

``_mask_url`` served a plain ``scheme://user:pw@host`` only: a ``?password=``
query value, the second password of a ``;``-separated kombu broker list and the
tail of a password holding a raw ``@`` were printed in full.
"""

from __future__ import annotations

import pytest

from sandloom.core.cli import url_text
from sandloom.core.migrations.backup import _postgres_item, _redis_item
from sandloom.core.migrations.migrations import _mask_dsns, _mask_url
from sandloom.core.migrations.url_mask import mask_query_secrets, mask_url_credentials

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
def test_the_backup_plan_prints_no_secret_of_a_redis_target(
    url: str, secret: str, masked: str
) -> None:
    item = _redis_item(url, ["cache:default"])
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


def test_a_semicolon_in_a_query_value_is_masked_unless_another_url_follows() -> None:
    assert mask_query_secrets("redis://h/0?password=a;b") == "redis://h/0?password=***"
    assert (
        mask_query_secrets("redis://h/0?password=a;redis://h2/0")
        == "redis://h/0?password=***;redis://h2/0"
    )


def test_the_cli_and_the_migration_runner_mask_with_one_masker() -> None:
    """``cli.url_text`` re-exports the leaf the runner imports: one masker, so a
    leak fixed there is fixed in ``sandloom config``, ``doctor`` and the plan."""
    assert url_text.mask_url_credentials is mask_url_credentials
    assert url_text.mask_query_secrets is mask_query_secrets
