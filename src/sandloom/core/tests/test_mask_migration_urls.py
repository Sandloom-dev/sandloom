"""The masker of ``sandloom backup plan`` and the migration logs hides every secret.

``_mask_url`` served a plain ``scheme://user:pw@host`` only: a ``?password=``
query value, the second password of a ``;``-separated kombu broker list and the
tail of a password holding a raw ``@`` were printed in full.
"""

from __future__ import annotations

import pytest

from sandloom.core.cli.url_text import mask_query_secrets
from sandloom.core.migrations.backup import _postgres_item, _redis_item
from sandloom.core.migrations.migrations import _mask_dsns, _mask_url

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


def test_the_migration_status_error_masks_a_raw_at_and_a_query_password() -> None:
    text = "connect failed: postgresql://u:pa@ss-hunter2@db/x?password=other-hunter2"
    masked = _mask_dsns(text)
    assert "hunter2" not in masked
    assert "@db/x" in masked


def test_mask_query_secrets_masks_a_value_and_leaves_the_rest() -> None:
    assert mask_query_secrets("x?a=1&token=hunter2&b=2") == "x?a=1&token=***&b=2"
