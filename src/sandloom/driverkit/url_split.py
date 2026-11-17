"""``split_url`` for a driver package: ``urlsplit`` whose error never carries the
url (it may hold a password). One implementation, in the kernel; a driver imports
it from here (ADR-0016: a driver package imports only ``sandloom.driverkit`` and
``sandloom.sdk``)."""

from __future__ import annotations

from sandloom.core.infra.url_split import split_url

__all__ = ["split_url"]
