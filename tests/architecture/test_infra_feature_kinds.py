"""Every feature a kernel capability declares has a kind (ADR-0016, spec 2.2).

A feature is a ``behaviour`` (an optional behaviour of a call every driver has:
without it the call raises ``Unsupported``) or a ``guarantee`` (a property no
call shows). A required operation is never a feature, and a speed claim is a
``performance_hint`` on the driver class, never a feature.
"""

from __future__ import annotations

import pytest

from sandloom.core.infra.capabilities import BUILTIN_CAPABILITIES
from sandloom.core.infra.extra_capabilities import EXTRA_CAPABILITIES
from sandloom.core.infra.ports._base import FEATURE_KINDS, CapabilitySpec

_SPECS = (*BUILTIN_CAPABILITIES, *EXTRA_CAPABILITIES)

#: Words of a speed claim: such a feature belongs in performance_hints.
_SPEED_WORDS = ("round_trip", "fast", "fused", "batch")


@pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.name)
def test_every_feature_is_classified(spec: CapabilitySpec) -> None:
    assert not spec.unclassified_features(), (
        f"{spec.name}: give each feature a kind (behaviour or guarantee) in its "
        f"CapabilitySpec.feature_kinds: {sorted(spec.unclassified_features())}"
    )
    assert set(spec.feature_kinds.values()) <= FEATURE_KINDS


@pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.name)
def test_no_feature_is_a_required_operation_or_a_speed_claim(
    spec: CapabilitySpec,
) -> None:
    required = set(getattr(spec.provider_contract, "__abstractmethods__", ()))
    assert not spec.features & required, (
        f"{spec.name}: a required operation is not a feature"
    )
    speedy = {f for f in spec.features if any(w in f for w in _SPEED_WORDS)}
    assert not speedy, f"{spec.name}: speed claims are performance_hints: {speedy}"


@pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.name)
def test_every_capability_has_an_integer_major(spec: CapabilitySpec) -> None:
    assert isinstance(spec.major, int) and spec.major >= 1
