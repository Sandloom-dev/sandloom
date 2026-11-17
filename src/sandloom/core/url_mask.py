"""Credentials of a url inside a text, masked. Standard library only (leaf module).

The migration runner (:mod:`sandloom.core.migrations.migrations`) masks the
urls of its logs and of ``sandloom backup plan`` with it, the dev CLI
(:mod:`sandloom.core.cli.url_text`, which re-exports it) masks the urls of
``sandloom config`` and ``sandloom doctor``, and the log scrubber
(``sandloom.core.observability.guardrails.scrub_text``) masks every log line and
Sentry event with it: one masker, so a leak fixed here is fixed in all of them.
It is a leaf of the kernel layers (the bottom layer of ``.importlinter``) so that
all of them may import it.

A url can carry its password in places a pattern that stops at the first ``/``,
``@`` or space misses: an unencoded ``/`` or space in the password, a second
``@``, a ``;`` followed by ``x://``, a ``?password=`` query. So the credentials
of a url run from ``scheme://`` to the last ``@`` of the line, without crossing
another ``scheme://``, except across a ``;`` that follows a password that is
already open (``u:ab;b://c@h``, see :data:`_OPEN_USERINFO_START`). A text that has a
later ``@`` on its line (a mail address) is masked too far, never too short. A
secret-named query value is masked first, so a raw ``@`` inside it
(``?password=p@ss``) does not make the credentials run through its name.

Two shapes stay ambiguous and read as a url list: a password of digits only
before the ``;`` (``u:123;b://c@h`` reads ``u:123`` as host and port) and a
password that holds a raw ``@`` before the ``;`` (the first ``@`` ends the url).
A ``,`` after digits ends a port too, so ``redis://h1:6379,h2:6379;redis://u:pw@h3``
keeps its first two hosts; a password of digits and a ``,`` before the ``;`` reads
as a host list likewise.

:func:`split_url_list` joins at most :data:`MAX_CHAINED_SCHEMES` pieces into one
password or query value; the rest of a longer chain is kept as one part, so the
whole of it is masked from the last ``@`` (hosts of later urls of the list go too,
no secret stays), and a search of a growing piece is done at most that many times.
"""

from __future__ import annotations

import re

#: A scheme is at most 32 characters: a longer run is no scheme, and the bound
#: keeps the scan linear (an unbounded ``[A-Za-z0-9+.\-]*`` rescanned the rest of
#: a long word from every position: seconds on a 32 000 character token).
SCHEME = r"[A-Za-z][A-Za-z0-9+.\-]{0,31}://"
#: What can stand between ``scheme://`` and the ``@`` that ends the credentials.
USERINFO = rf"(?:(?!{SCHEME})[^\n])*"
#: ``user:`` of a url whose password may be open: not an IPv6 host.
_USER_AND_COLON = r"(?!\[)[^/?#@;:\s\[]*:"
#: A password that is already open before a ``;``: ``user:`` then something that
#: is not a port. ``h1:6379;redis://h2`` (two hosts of a list) is no password: the
#: text after the colon is digits up to the end of the piece (a ``/``, ``?``,
#: ``#``, a space, a ``,`` that goes on with the next host of
#: ``redis://h1:6379,h2:6379``, the ``;`` of the next url or the end).
_OPEN_USERINFO_START = rf"{_USER_AND_COLON}(?!\d*(?:[/?#\s,]|;\s*{SCHEME}|\Z))"

#: The text of a piece of an open password, up to the next ``;`` + scheme: it has
#: no ``@`` (the first ``@`` ends a url), so a list of urls that each have one is
#: never read as one password.
_OPEN_PIECE = rf"(?:(?!{SCHEME})[^\n@])*"
#: How many ``;x://`` pieces one open password may chain. Unbounded (``+``), every
#: ``y://u:p;`` of a line with no ``@`` scanned to the end of the line and failed,
#: and the engine retried at the next one: quadratic (12 s on 50 000 characters of
#: ``y://u:p;``, and ``scrub_text`` runs on every log line). With a bound each start
#: scans a constant number of pieces. A password that chains more than this is a
#: documented limit: the pieces before its last eight are printed.
MAX_CHAINED_SCHEMES = 8
_CREDENTIALS = re.compile(
    rf"({SCHEME})"
    rf"({_OPEN_USERINFO_START}(?:{_OPEN_PIECE};\s*{SCHEME}){{1,{MAX_CHAINED_SCHEMES}}}"
    rf"{USERINFO}|{USERINFO})@"
)
#: The name of a query parameter that ends in a secret word (``password``,
#: ``sslpassword``, ``access_token``, ``client_secret``, ``sig``, ``PASS``, ...)
#: with its ``=``. ``?``, ``&`` and ``;`` start a parameter (a kombu list and some
#: drivers separate them with ``;``). Masking too much (``monkey=``) is fine, too
#: little is not.
_QUERY_PARAM_HEAD = (
    r"[?&;][\w.\-]*?(?:password|passwd|pass|pwd|secret|token|key|sig|signature"
    r"|auth|credentials?)="
)
#: The value is masked up to the next ``&``, ``#`` or space: a ``;`` in it is part
#: of it, one that starts ``x://`` too (a password may hold it).
_QUERY_SECRET = re.compile(rf"({_QUERY_PARAM_HEAD})[^&#\s]*", re.IGNORECASE)
#: A piece of a url list that ends inside the value of a secret-named parameter.
_OPEN_QUERY_SECRET = re.compile(rf"{_QUERY_PARAM_HEAD}[^&#\s]*\Z", re.IGNORECASE)
#: A piece of a url list whose password is still open: no ``@`` after it.
_OPEN_PASSWORD = re.compile(rf"{SCHEME}{_USER_AND_COLON}(?!\d*(?:[/?#\s,]|\Z))[^@]*\Z")
_URL_LIST_SEPARATOR = re.compile(rf";(?=\s*{SCHEME})")
#: The password of an HTTP token login (``https://<token>:x-oauth-basic@host``):
#: the user slot holds the secret.
_OAUTH_BASIC_WORD = "x-oauth-basic"


def mask_url_credentials(text: str, *, keep_user: bool = True) -> str:
    """``scheme://user:pw@host`` -> ``scheme://user:***@host`` anywhere in text.

    A secret-named parameter of a query (``password=``, ``access_token=``, ...)
    becomes ``***`` too. The user stays where it is a role name; it goes with the
    password (``scheme://***@host``) when the password is empty or
    ``x-oauth-basic`` (the user slot holds a token or a key then), and always with
    ``keep_user=False``. The query secrets are masked first (see the module
    docstring).
    """
    return mask_url_userinfo(mask_query_secrets(text), keep_user=keep_user)


def mask_url_userinfo(text: str, *, keep_user: bool = True) -> str:
    """The userinfo of every url of ``text`` masked, the query left as it is.

    :func:`mask_url_credentials` without the query step, for a caller that has
    masked the query already (``scrub_text`` masks ``key=value`` pairs and query
    secrets before the credentials, so ``password=hunter@2`` is gone before the
    userinfo of the url in front of it runs to the last ``@``).
    """

    def _sub(match: re.Match[str]) -> str:
        user, _, password = match.group(2).partition(":")
        if not keep_user or not password or password == _OAUTH_BASIC_WORD:
            user = ""
        return f"{match.group(1)}{user + ':' if user else ''}***@"

    return _CREDENTIALS.sub(_sub, text)


def _is_open(piece: str) -> bool:
    """Whether ``piece`` ends inside a password or a secret query value.

    An open password has no ``@`` after it, so only the text after the last ``@``
    is searched: with every ``x://u:p`` start of ``x://u:px://u:p...@`` scanning on
    to that ``@`` and failing, the search of the whole piece was quadratic."""
    return bool(
        _OPEN_PASSWORD.search(piece[piece.rfind("@") + 1 :])
        or _OPEN_QUERY_SECRET.search(piece)
    )


def split_url_list(url: str) -> list[str]:
    """The urls of a ``;``-separated kombu list. Only a ``;`` that starts another
    ``scheme://`` separates, and not one inside a password that is still open
    (``u:ab;b://c@h``) or inside the value of a secret-named query parameter
    (``?password=ab;b://c``): that ``;`` is part of the secret, and printing the
    next piece on its own would print the end of it.

    At most :data:`MAX_CHAINED_SCHEMES` separators are joined into one piece (the
    open piece is searched again at each one, so an unbounded chain was quadratic:
    4 s on 50 000 characters of ``;sig=a;x://``). A longer chain is not split at
    all from there on: the rest of the url is one part, masked whole."""
    parts: list[str] = []
    start = 0
    joined = 0
    for separator in _URL_LIST_SEPARATOR.finditer(url):
        piece = url[start : separator.start()]
        if _is_open(piece):
            joined += 1
            if joined > MAX_CHAINED_SCHEMES:
                break
            continue
        parts.append(piece)
        start = separator.end()
        joined = 0
    parts.append(url[start:])
    return parts


def mask_query_secrets(text: str) -> str:
    """The value of a secret-named query parameter of ``text`` becomes ``***``."""
    return _QUERY_SECRET.sub(r"\1***", text)
