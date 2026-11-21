"""``urlsplit`` for a url a person typed, whose error never carries the url.

``urllib.parse.urlsplit`` raises a ``ValueError`` whose message holds the netloc
when it contains a character that NFKC-normalises to ``:``, ``/``, ``?``, ``#``
or ``@`` (for example U+FF1A). The netloc has the ``user:password``, and the
message reaches an error line, a log or a validation error. :func:`split_url`
raises one fixed message instead and drops the chained error. :func:`redact`
is the url for a message: the password left out.
"""

from __future__ import annotations

import re
from urllib.parse import SplitResult, urlsplit

from sandloom.core.url_mask import SCHEME, mask_query_secrets, mask_url_credentials

__all__ = ["redact", "split_url"]

_HAS_SCHEME = re.compile(SCHEME)


def split_url(url: str) -> SplitResult:
    """``urlsplit(url)``; a url it refuses raises ``ValueError`` with a message
    that does not contain the url (it may hold a password)."""
    try:
        return urlsplit(url)
    except ValueError:
        raise ValueError(
            "the url cannot be parsed (its text is left out: it may hold a "
            "password): write its host and credentials in plain ASCII (no "
            "character that stands for ':', '/', '?', '#' or '@' under Unicode "
            "normalization) and close the brackets of an IPv6 host"
        ) from None


def redact(url: str) -> str:
    """``url`` with its password replaced by ``<password>`` (for messages). A url
    that cannot be parsed, or has no ``scheme://``, is not echoed at all: it may
    hold the password (``urlsplit`` reads ``user:pw@host:port`` as no netloc and no
    password, and ends the netloc at an unencoded ``/`` of the password)."""
    if not _HAS_SCHEME.match(url):
        return "<url without a scheme>"
    try:
        parts = split_url(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.password:
        return mask_url_credentials(url)
    netloc = parts.netloc.replace(f":{parts.password}@", ":<password>@", 1)
    return mask_query_secrets(parts._replace(netloc=netloc).geturl())
