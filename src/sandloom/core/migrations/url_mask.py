"""Credentials of a url inside a text, masked. Standard library only (leaf module).

The migration runner (:mod:`sandloom.core.migrations.migrations`) masks the
urls of its logs and of ``sandloom backup plan`` with it, and the dev CLI
(:mod:`sandloom.core.cli.url_text`, which re-exports it) masks the urls of
``sandloom config`` and ``sandloom doctor``: one masker, so a leak fixed here
is fixed in all of them. The kernel layers put ``migrations`` below ``cli``,
which is why it lives here.

A url can carry its password in places a pattern that stops at the first ``/``,
``@`` or space misses: an unencoded ``/`` or space in the password, a second
``@``, a ``?password=`` query. So the credentials of a url run from
``scheme://`` to the last ``@`` of the line, without crossing another
``scheme://``. A text that has a later ``@`` on its line (a mail address) is
masked too far, never too short.
"""

from __future__ import annotations

import re

SCHEME = r"[A-Za-z][A-Za-z0-9+.\-]*://"
#: What can stand between ``scheme://`` and the ``@`` that ends the credentials.
USERINFO = rf"(?:(?!{SCHEME})[^\n])*"

_CREDENTIALS = re.compile(rf"({SCHEME})({USERINFO})@")
#: A query parameter whose NAME ends in a secret word (``password``,
#: ``sslpassword``, ``access_token``, ``client_secret``, ``sig``, ``PASS``, ...):
#: its value is masked up to the next ``&``, ``#``, space or ``;`` that starts
#: another url (a ``;`` inside a value is part of it). ``?``, ``&`` and ``;`` start
#: a parameter (a kombu list and some drivers separate them with ``;``). Masking
#: too much (``monkey=``) is fine, too little is not.
_QUERY_SECRET = re.compile(
    r"([?&;][\w.\-]*?(?:password|passwd|pass|pwd|secret|token|key|sig|signature"
    r"|auth|credentials?)=)(?:(?!;\s*" + SCHEME + r")[^&#\s])*",
    re.IGNORECASE,
)
_URL_LIST_SEPARATOR = re.compile(rf";(?=\s*{SCHEME})")
#: The password of an HTTP token login (``https://<token>:x-oauth-basic@host``):
#: the user slot holds the secret.
_TOKEN_LOGIN_PASSWORD = "x-oauth-basic"  # noqa: S105 - a fixed word, no secret


def mask_url_credentials(text: str, *, keep_user: bool = True) -> str:
    """``scheme://user:pw@host`` -> ``scheme://user:***@host`` anywhere in text.

    A secret-named parameter of a query (``password=``, ``access_token=``, ...)
    becomes ``***`` too. The user stays where it is a role name; it goes with the
    password (``scheme://***@host``) when the password is empty or
    ``x-oauth-basic`` (the user slot holds a token or a key then), and always with
    ``keep_user=False``.
    """

    def _sub(match: re.Match[str]) -> str:
        user, _, password = match.group(2).partition(":")
        if not keep_user or not password or password == _TOKEN_LOGIN_PASSWORD:
            user = ""
        return f"{match.group(1)}{user + ':' if user else ''}***@"

    return mask_query_secrets(_CREDENTIALS.sub(_sub, text))


def split_url_list(url: str) -> list[str]:
    """The urls of a ``;``-separated kombu list. Only a ``;`` that starts another
    ``scheme://`` separates: any other ``;`` is part of a password or a query."""
    return _URL_LIST_SEPARATOR.split(url)


def mask_query_secrets(text: str) -> str:
    """The value of a secret-named query parameter of ``text`` becomes ``***``."""
    return _QUERY_SECRET.sub(r"\1***", text)
