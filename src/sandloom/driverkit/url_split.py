"""``split_url`` and ``redact`` for a driver package: ``urlsplit`` whose error never
carries the url (it may hold a password), and the url for a message with its
password left out. One implementation, in the kernel; a driver imports it from
here (ADR-0016: a driver package imports only ``sandloom.driverkit`` and
``sandloom.sdk``)."""

from __future__ import annotations

from sandloom.core.url_split import redact, split_url

__all__ = ["redact", "split_url"]
