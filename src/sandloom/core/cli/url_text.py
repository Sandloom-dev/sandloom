"""Urls inside a text, masked or dropped. Standard library only.

The dev CLI reads no settings, so this module imports none: ``cli.config`` and
``cli.doctor`` mask urls through it, and the ``dev up`` failure line drops them.

The masker is :mod:`sandloom.core.url_mask` (the migration runner masks
its logs and ``sandloom backup plan`` with the same one), re-exported here; its
docstring says where a password of a url can stand.
"""

from __future__ import annotations

import re

from sandloom.core.url_mask import (
    SCHEME,
    USERINFO,
    mask_query_secrets,
    mask_url_credentials,
)

_WHOLE_URL = re.compile(rf"{SCHEME}(?:{USERINFO}@)?\S*")

URL_DROPPED = "<url>"

__all__ = ["URL_DROPPED", "drop_urls", "mask_query_secrets", "mask_url_credentials"]


def drop_urls(text: str) -> str:
    """Every url of ``text`` replaced by ``<url>``: host, path and query go too."""
    return _WHOLE_URL.sub(URL_DROPPED, text)
