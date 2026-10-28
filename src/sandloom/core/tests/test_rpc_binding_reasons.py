"""Every response-binding rejection reason is declared (review GLM-6).

``REJECT_REASONS`` is the documented label set of
``sandloom_rpc_response_binding_rejected_total{reason}`` (dashboards, alerts,
tests summing over it). A reason :func:`rpc_binding.verify` emits but the
tuple omits — the removed ``token_alg_refused`` was one — silently escapes
them.
"""

from __future__ import annotations

import ast
import inspect

from sandloom.core import rpc_binding


def _verify_reasons() -> set[str]:
    """Every string literal :func:`rpc_binding.verify` returns."""
    tree = ast.parse(inspect.getsource(rpc_binding.verify))
    return {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def test_every_reason_verify_emits_is_declared() -> None:
    emitted = _verify_reasons()
    assert {"unkeyed", "mac_mismatch", "service_mismatch"} <= emitted  # scan works
    assert emitted <= set(rpc_binding.REJECT_REASONS)


def test_reject_reasons_are_unique() -> None:
    assert len(set(rpc_binding.REJECT_REASONS)) == len(rpc_binding.REJECT_REASONS)
