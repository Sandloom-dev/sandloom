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
import time

import pytest

from sandloom.core.observability.guardrails import scrub_text
from sandloom.core.url_mask import (
    mask_query_secrets,
    mask_url_credentials,
    mask_url_userinfo,
)

# ---- reference: independent unconditional security substitutions ---------
# The url credentials and the query secrets are the shared masker of
# ``sandloom.core.url_mask`` (one pattern, no copy here); the reference runs it
# unconditionally, query first and credentials last, like ``scrub_text``.
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


def _ref_mask(match: re.Match[str]) -> str:
    # A module declaration's boolean (``issues_tokens=True``) is no secret;
    # any other value under that key, and every other key, is masked.
    if match["key"].lower() == "issues_tokens" and match["value"].rstrip(
        "."
    ).lower() in {"true", "false"}:
        return match[0]
    return f"{match['key']}{match['sep']}***"


def reference_scrub(text: str) -> str:
    if not text:
        return text
    out = mask_query_secrets(text)
    out = _REF_PROVIDER.sub("***", out)
    out = _REF_SQL.sub("[parameters: ***]", out)
    out = _REF_BEARER.sub(r"\1 ***", out)
    out = _REF_KV.sub(_ref_mask, out)
    return mask_url_userinfo(out, keep_user=False)


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
    "?",
    "?password=p4ss",
    "&sig=abc123",
    ";key=k3y",
    "?sslpassword=ssl-secret;redis://",
    "user:p@ss@",
    "http://u:p/ss w@",
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
    "issues_tokens",
    "ISSUES_TOKENS",
    "True",
    "false",
    "None",
    "123456",
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


def test_an_issuer_declaration_reads_as_written() -> None:
    """A B035 refusal's ``issues_tokens=True`` is a declaration, not a secret:
    the ERROR log line says what is wrong, like the CLI's stderr."""
    for text in (
        "module 'acme.idp' declares issues_tokens=True, but the map names no issuer",
        "issues_tokens=False.",
        'register(issues_tokens="True")',
    ):
        assert scrub_text(text) == text


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        # sandloom: secret-fixture — synthetic redaction test values
        ("issues_tokens=s3cr3t-value", "s3cr3t-value"),
        ("issues_tokens=Truely-a-secret", "Truely-a-secret"),
        ("x_issues_tokens=True", "True"),
        ("access_token=True", "True"),
        ("refresh_token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig", "eyJhbGciOiJ"),
        ("token=482913", "482913"),
        ("password=123456", "123456"),
        ("password: None", "None"),
        ("api_key=False", "False"),
        ("client_secret='hunter2'", "hunter2"),
    ],
)
def test_secret_values_are_still_masked(raw: str, secret: str) -> None:
    """The declaration exemption is the exact key with a boolean only: every
    other value, digits and booleans under any other key, stays masked."""
    out = scrub_text(raw)
    assert secret not in out, out
    assert "***" in out


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        # sandloom: secret-fixture — synthetic redaction test values
        ("postgresql://u:p@ss@h/db", "ss"),
        ("postgresql://u:p/ss@h/db", "ss"),
        ("postgresql://u:p ss@h/db", "ss"),
        ("redis://h/0?password=ab;cd&x=1", "cd"),
        ("redis://h/0?password=a)b", "b"),
        ("redis://h/0?sig=abc123&x=1", "abc123"),
        ("amqp://h/?auth=s3cr3t", "s3cr3t"),
        ("connect to /db?sslkey=/etc/k3y.pem failed", "k3y"),
        ("redis://h1;redis://u:pw0rd@h2", "pw0rd"),
        ("https://tok3n:x-oauth-basic@h", "tok3n"),
        # a ';' followed by 'x://' inside a password or a query value.
        ("postgresql://u:pw1;kw://pw2@h/db", "pw1"),
        ("postgresql://u:pw1;kw://pw2@h/db", "pw2"),
        ("redis://h/0?password=ab;cd://x", "cd://x"),
        ("redis://h/0?password=ab;cd://x", "ab"),
    ],
)
def test_url_credentials_are_hidden_whole(raw: str, secret: str) -> None:
    """The userinfo runs to the last ``@`` and a secret-named query value is
    masked to its end: a raw ``@``, ``/``, space or ``;`` in it does not leave a tail."""
    out = scrub_text(raw)
    assert secret not in out, out
    assert "***" in out
    assert out == reference_scrub(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # sandloom: secret-fixture — synthetic redaction test values
        # A raw '@' is legal in a query: the userinfo of the url in front of it
        # must not run to that '@' and eat the name of the parameter.
        ("redis://h:6379/0?password=p@ss", "redis://h:6379/0?password=***"),
        (
            "postgresql://u@h/db?sslpassword=ab@cd",
            "postgresql://***@h/db?sslpassword=***",
        ),
        (
            "connect url=postgresql://db:5432/app password=hunter@2",
            "connect url=postgresql://db:5432/app password=***",
        ),
        ("redis://u:p@ss@h/0?token=x@y", "redis://***@h/0?token=***"),
    ],
)
def test_a_raw_at_after_a_url_does_not_hide_the_name_of_a_secret(
    raw: str, expected: str
) -> None:
    """The ``key=value`` and query steps run before the credentials: their names
    are still there when the userinfo runs to the last ``@`` of the line."""
    assert scrub_text(raw) == expected
    assert reference_scrub(raw) == expected


#: The names the query step masks (``url_mask._QUERY_PARAM_HEAD``), each in
#: lower and upper case, alone and behind a prefix: removing one from the
#: pattern prints its value through ``scrub_text``.
_SECRET_QUERY_NAMES = [
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
]


@pytest.mark.parametrize("name", _SECRET_QUERY_NAMES)
@pytest.mark.parametrize("variant", ["plain", "prefixed", "upper"])
def test_every_secret_query_name_is_masked_by_the_scrubber(
    name: str, variant: str
) -> None:
    param = {"plain": name, "prefixed": f"ssl_{name}", "upper": name.upper()}[variant]
    for sep in ("?", "&", ";"):
        # A url without ``@`` and without a ``key=value`` shape the other steps
        # would mask: only the query step is under test.
        raw = f"redis://h/0{sep}x=1{sep}{param}=zzTOPSECRETzz"
        assert scrub_text(raw) == f"redis://h/0{sep}x=1{sep}{param}=***", raw
        assert mask_query_secrets(raw) == f"redis://h/0{sep}x=1{sep}{param}=***", raw


#: Urls on which the scrubber and the masker of ``sandloom config`` / the migration
#: logs (``sandloom.core.url_mask``) must print the same text: ``scrub_text`` runs
#: that masker, and these urls show that its other steps leave them as they are.
_SHARED_URLS = [
    "postgresql://u:p@ss@h/db",
    "postgresql://u:p/ss@h/db",
    "redis://:pw@h:6379/0",
    "redis://h/0?password=ab;cd&x=1",
    "redis://h/0?password=a)b",
    "redis://h/0?sig=abc123&key=zzz",
    "amqp://h/?auth=abc",
    "postgresql://u:pw@h/db?sslpassword=xx",
    "redis://h1;redis://u:pw@h2",
    "redis://h1:6379;redis://h2:6379/1",
    "redis://h1:6379;redis://u:pw@h2",
    "redis://[::1]:6379;redis://u:pw@h2",
    "amqp://u:p@h1;amqp://u:p@h2",
    "redis://u:ab;cd@h1/0;redis://v:ef;gh@h2/0",
    "postgresql://u:pw1;kw://pw2@h/db",
    "postgresql://u:pw1; kw://pw2@h/db",
    "postgresql://u:a;b://c;d://e@h/db",
    "redis://h/0?password=ab;cd://x",
    "redis://h/0?password=a;redis://h2/0",
    "https://tok:x-oauth-basic@h",
    "http://host/path no credentials",
]


@pytest.mark.parametrize("raw", _SHARED_URLS)
def test_the_scrubber_and_the_url_masker_print_the_same_url(raw: str) -> None:
    assert scrub_text(raw) == mask_url_credentials(raw, keep_user=False)


def test_a_long_run_after_a_scheme_is_linear() -> None:
    """A letter-and-digit run in a text that holds ``://`` was rescanned from
    every position (quadratic: a 32 000 character token took seconds)."""
    text = "GET http://h/" + "a" * 60_000
    started = time.perf_counter()
    assert scrub_text(text) == text
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
    """The open-password branch chains ``;x://`` pieces; unbounded, every start
    scanned to the end of a line without ``@``: 12 s on 50 000 characters. The
    chain is capped (``url_mask.MAX_CHAINED_SCHEMES``)."""
    started = time.perf_counter()
    scrub_text(text)
    assert time.perf_counter() - started < 1.0


def test_long_identifier_is_linear() -> None:
    # The original pattern rescanned every position of a long [\w-] run; the
    # anchored one is linear. Equality is the contract; this just exercises it.
    text = ("a" * 3000) + " password=x " + ("b-" * 1500)
    assert scrub_text(text) == reference_scrub(text)
