"""Urls inside a text, masked or dropped. Standard library only.

The dev CLI reads no settings, so this module imports none: ``cli.config`` and
``cli.doctor`` mask urls through it, and the ``dev up`` failure line drops them.

A url can carry its password in places a pattern that stops at the first ``/``,
``@`` or space misses: an unencoded ``/`` or space in the password, a second
``@``, a ``?password=`` query. So the credentials of a url run from
``scheme://`` to the last ``@`` of the line, without crossing another
``scheme://``. A text that has a later ``@`` on its line (a mail address) is
masked too far, never too short.
"""

from __future__ import annotations

import re

_SCHEME = r"[A-Za-z][A-Za-z0-9+.\-]*://"
#: What can stand between ``scheme://`` and the ``@`` that ends the credentials.
_USERINFO = rf"(?:(?!{_SCHEME})[^\n])*"

_CREDENTIALS = re.compile(rf"({_SCHEME})({_USERINFO})@")
_WHOLE_URL = re.compile(rf"{_SCHEME}(?:{_USERINFO}@)?\S*")
_QUERY_SECRET = re.compile(
    r"([?&;](?:password|passwd|pwd|secret|token|key|api_key|apikey|access_key"
    r"|auth|credentials?)=)[^&#\s]*",
    re.IGNORECASE,
)

URL_DROPPED = "<url>"


def mask_url_credentials(text: str) -> str:
    """``scheme://user:pw@host`` -> ``scheme://user:***@host`` anywhere in text.

    A ``password=`` (or token, key, secret) of a query becomes ``***`` too.
    """

    def _sub(match: re.Match[str]) -> str:
        userinfo = match.group(2)
        user = userinfo.split(":", 1)[0] if ":" in userinfo else ""
        return f"{match.group(1)}{user + ':' if user else ''}***@"

    return mask_query_secrets(_CREDENTIALS.sub(_sub, text))


def mask_query_secrets(text: str) -> str:
    """The value of a ``password=`` (or token, key, secret) query parameter of
    ``text`` becomes ``***``; a ``;`` starts a parameter too (a kombu list)."""
    return _QUERY_SECRET.sub(r"\1***", text)


def drop_urls(text: str) -> str:
    """Every url of ``text`` replaced by ``<url>``: host, path and query go too."""
    return _WHOLE_URL.sub(URL_DROPPED, text)
