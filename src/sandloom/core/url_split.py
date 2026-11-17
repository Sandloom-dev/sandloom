"""``urlsplit`` for a url a person typed, whose error never carries the url.

``urllib.parse.urlsplit`` raises a ``ValueError`` whose message holds the netloc
when it contains a character that NFKC-normalises to ``:``, ``/``, ``?``, ``#``
or ``@`` (for example U+FF1A). The netloc has the ``user:password``, and the
message reaches an error line, a log or a validation error. :func:`split_url`
raises one fixed message instead and drops the chained error.
"""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit

__all__ = ["split_url"]


def split_url(url: str) -> SplitResult:
    """``urlsplit(url)``; a url it refuses raises ``ValueError`` with a message
    that does not contain the url (it may hold a password)."""
    try:
        return urlsplit(url)
    except ValueError:
        raise ValueError(
            "the url cannot be parsed (its text is left out: it may hold a password)"
        ) from None
