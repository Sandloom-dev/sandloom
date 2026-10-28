"""Non-ASCII header values never crash a MAC comparison (review hand-off).

``hmac.compare_digest(str, str)`` raises ``TypeError`` when either string is
not ASCII, and an HTTP header byte >= 0x80 arrives latin-1 decoded: a forged
request tag / signature / response MAC turned a clean rejection into an
unhandled 500 (or, on the caller side, a crash instead of a ``mac_mismatch``
verdict). Every such value is now rejected without a ``TypeError``.
"""

from __future__ import annotations

from typing import Any

import pytest

from sandloom.core import rpc_binding, rpc_identity, rpc_pair_mac
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import rpc_request_headers, rpc_settings
from sandloom.core.rpc.wire import _CTX_HEADER, _NONCE_HEADER

_TARGET = "svc.v1/method"
_BODY = b"{}"


@pytest.fixture
def cfg() -> Any:
    return settings.model_copy(
        update=rpc_settings("svc", allowed_calls={"svc": ["svc.v1"]})
    )


def test_pair_mac_tag_with_non_ascii_value_is_rejected_cleanly(cfg: Any) -> None:
    headers = rpc_request_headers(cfg, _TARGET, _BODY)
    raw = headers[rpc_pair_mac.AUTH_HEADER]
    forged = raw[: raw.index("mac=") + 4] + "\xe9" * 43
    with pytest.raises(rpc_pair_mac.AuthError) as err:
        rpc_pair_mac.verify_request(
            cfg,
            forged,
            http_method="POST",
            target=_TARGET,
            nonce=headers[_NONCE_HEADER],
            context=headers[_CTX_HEADER],
            body=_BODY,
        )
    assert err.value.status_code == 401


def test_command_signature_with_non_ascii_value_is_rejected_cleanly(
    cfg: Any,
) -> None:
    headers = rpc_request_headers(cfg, _TARGET, _BODY, sign=True)
    assert headers.get(rpc_identity.SVC_SIG_HEADER)
    with pytest.raises(rpc_identity.ServiceIdentityError):
        rpc_identity.verify(
            cfg,
            http_method="POST",
            target=_TARGET,
            body=_BODY,
            context=headers[_CTX_HEADER],
            signature="\xe9" * 86,
        )


def test_response_mac_with_non_ascii_value_is_a_mismatch(cfg: Any) -> None:
    nonce = "n" * 22
    own = rpc_binding.own_dh_key(cfg)
    assert own is not None
    headers = {
        rpc_binding.NONCE_HEADER: nonce,
        rpc_binding.BINDING_HEADER: rpc_binding.descriptor(own.kid, own.service_id),
        rpc_binding.MAC_HEADER: "\xe9" * 64,
    }
    reason = rpc_binding.verify(
        cfg,
        headers,
        nonce=nonce,
        sent={},
        expect_service="svc",
        status_code=200,
        body=_BODY,
    )
    assert reason == "mac_mismatch"
