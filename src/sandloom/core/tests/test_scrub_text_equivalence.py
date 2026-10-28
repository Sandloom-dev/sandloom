"""``scrub_text`` prefilters match an independent unconditional reference.

The reference includes the reviewed security contract: password-only Redis
DSNs and provider-prefixed secrets must also be removed. Apart from those
explicitly stronger redactions, optimisation preserves output; a seeded generator builds thousands of
strings mixing secrets, DSNs, bearer tokens, ``key=value`` pairs, SQLAlchemy
``[parameters: ...]`` blocks, long identifiers, tracebacks and non-ASCII
case-folding traps.
"""

from __future__ import annotations

import random
import re

import pytest

from sandloom.core.observability.guardrails import scrub_text

# ---- reference: independent unconditional security substitutions ---------
_REF_DSN = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s/@:]*:[^\s/@]+@")
_REF_PROVIDER = re.compile(
    r"\b(?:sk_(?:live|test)_|rk_(?:live|test)_|whsec_)[A-Za-z0-9_]+"
)
_REF_SQL = re.compile(r"\[parameters: .*?\](?=\s*(?:\n|\(Background|$))", re.S)
_REF_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*")
_REF_KV = re.compile(
    r"(?i)(?P<key>[\w\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|"
    r"authorization|credential|private[_\-]?key|cookie)[\w\-]*)"
    r"(?P<sep>[\"']?\s*[=:]\s*[\"']?)(?P<value>[^\s,;&\"'\)\]}]+)"
)


def reference_scrub(text: str) -> str:
    if not text:
        return text
    out = _REF_DSN.sub(r"\g<scheme>***@", text)
    out = _REF_PROVIDER.sub("***", out)
    out = _REF_SQL.sub("[parameters: ***]", out)
    out = _REF_BEARER.sub(r"\1 ***", out)
    return _REF_KV.sub(r"\g<key>\g<sep>***", out)


# ---- corpus -----------------------------------------------------------------
_FRAGMENTS = [
    "password",
    "Password",
    "PASSWD",
    "pwd",
    "db_password",
    "x-api-key",
    "api_key",
    "apikey",
    "API-KEY",
    "client_secret",
    "stripe_secret_key",
    "token",
    "access_token",
    "refresh-token",
    "authorization",
    "Authorization",
    "credential",
    "private_key",
    "privatekey",
    "set-cookie",
    "cookie",
    "Bearer",
    "bearer",
    "BEARER",
    "=",
    ":",
    " = ",
    ": ",
    '"',
    "'",
    '="',
    "':'",
    " ",
    "  ",
    "\n",
    "\t",
    ",",
    ";",
    "&",
    ")",
    "]",
    "}",
    "(",
    "[",
    "{",
    "-",
    "_",
    "@",
    "/",
    "://",
    "postgresql+asyncpg://",
    "redis://",
    "redis://:synthetic-password@cache:6379/0",
    # sandloom: secret-fixture — synthetic redaction test token
    "sk_live_syntheticproviderkey",
    "whsec_syntheticwebhookkey",
    "user:pa55@",
    "admin:s3cr3t@db.local:5432/app",
    "host/path",
    "[parameters: ",
    "[parameters: ('a@b.c', 'tok')]",
    "(Background on this error at: https://sqlalche.me/e/20/gkpj)",
    "abc",
    "XYZ123",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
    "a1b2c3d4==",
    "~+/.",
    "the quick brown fox",
    "Traceback (most recent call last):",
    '  File "/app/x.py", line 12, in f',
    "ValueError: bad",
    "some_very_long_identifier_without_secrets_" * 3,
    "tokenizer",
    "pwdless",
    "secretary",
    "apiary",
    "privately",
    "cookies",
    # non-ASCII and case-folding traps (U+212A KELVIN SIGN folds to 'k',
    # U+017F LONG S folds to 's', U+0130 to 'i')
    "K",  # noqa: RUF001 - homoglyph under test
    "paſſword",  # noqa: RUF001 - homoglyph under test
    "İ",
    "toKen",
    "coKie",  # noqa: RUF001 - homoglyph under test
    "été",
    "пароль",
    "ı",  # noqa: RUF001 - homoglyph under test
]


def _corpus(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    out: list[str] = [
        "",
        "plain message",
        "password=hunter2",
        "Authorization: Bearer abc.def.ghi",
        "postgresql://u:p@h/db",
        "[parameters: ('x',)]\n(Background on this error at: z)",
        "x" * 5000 + "password=1",
        ("word_" * 400) + "token: abc",
    ]
    for _ in range(n):
        parts = rng.randint(1, 18)
        out.append("".join(rng.choice(_FRAGMENTS) for _ in range(parts)))
    return out


@pytest.mark.parametrize("seed", range(8))
def test_scrub_text_matches_reference(seed: int) -> None:
    for text in _corpus(1500, seed):
        assert scrub_text(text) == reference_scrub(text), repr(text)


def test_scrub_text_still_redacts() -> None:
    raw = (
        "connect postgresql+asyncpg://admin:s3cr3t@db:5432/app failed; "
        "Authorization: Bearer eyJ.abc.def api_key=XYZ "
        "[parameters: ('a@b.c', 'tok')]\n(Background on this error at: x)"
    )
    out = scrub_text(raw)
    assert "s3cr3t" not in out
    assert "eyJ.abc.def" not in out
    assert "XYZ" not in out
    assert "a@b.c" not in out
    assert out == reference_scrub(raw)


def test_long_identifier_is_linear() -> None:
    # The original pattern rescanned every position of a long [\w-] run; the
    # anchored one is linear. Equality is the contract; this just exercises it.
    text = ("a" * 3000) + " password=x " + ("b-" * 1500)
    assert scrub_text(text) == reference_scrub(text)
