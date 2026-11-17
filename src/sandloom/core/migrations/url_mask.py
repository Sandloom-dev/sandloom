"""The url masker moved to :mod:`sandloom.core.url_mask` (a leaf of the kernel layers
that the log scrubber imports too); this module re-exports it for the importers
that still name the old path."""

from __future__ import annotations

from sandloom.core.url_mask import (
    MAX_CHAINED_SCHEMES,
    SCHEME,
    USERINFO,
    mask_query_secrets,
    mask_url_credentials,
    mask_url_userinfo,
    split_url_list,
)

__all__ = [
    "MAX_CHAINED_SCHEMES",
    "SCHEME",
    "USERINFO",
    "mask_query_secrets",
    "mask_url_credentials",
    "mask_url_userinfo",
    "split_url_list",
]
