"""The required-backend guard of a driver package's test lane (spec 2.7).

A driver package tests against a real backend (a Redis, a broker, a bucket). Its
CI lane sets one variable, ``SANDLOOM_<PACKAGE>_REQUIRED=1``: a backend that did
not start must turn the lane red, not green with every test skipped. Elsewhere
(a laptop without the backend) the same tests skip. Each package keeps its own
probe and calls :func:`required_or_skip` where the probe finds nothing, never
``pytest.skip`` directly; import the names from
:mod:`sandloom.driverkit.testing`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import NoReturn

import pytest

#: A variable name of the lane: upper-case letters, digits and underscores.
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


def _checked_env_name(env_var: str) -> str:
    if not _ENV_NAME.fullmatch(env_var):
        raise ValueError(
            f"env_var must be an upper-case variable name, got {env_var!r}"
        )
    return env_var


def lane_required(env_var: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether the lane must have its backend: ``env_var`` is exactly ``"1"``
    in ``environ`` (default: the process environment). ``"0"``, ``"true"`` and
    an empty value do not require it."""
    values = os.environ if environ is None else environ
    return values.get(_checked_env_name(env_var)) == "1"


def required_or_skip(env_var: str, reason: str) -> NoReturn:
    """Fail when ``env_var`` is ``"1"`` (the lane requires its backend), else
    skip, with ``reason``.

    The message names the variable and ``reason``. ``reason`` states what is
    missing (``"test Redis not reachable"``); it must carry no URL, host or
    credential: pass the exception type, not its text.
    """
    _checked_env_name(env_var)
    if not reason.strip():
        raise ValueError("reason must say what is missing")
    if lane_required(env_var):
        pytest.fail(f"{reason} ({env_var}=1: this lane may not skip)", pytrace=False)
    pytest.skip(f"{reason} ({env_var}=1 would make this an error)")
