"""Wire framing shared by both sides: MessagePack bodies, base64url, the
RPC header names and the request body model.
"""

from __future__ import annotations

import base64
from typing import Any

import msgpack
from pydantic import BaseModel, ConfigDict, Field

from sandloom.core import contract_wire, rpc_binding

# Internal RPC is binary MessagePack on the wire: smaller and faster to
# (de)serialize than JSON, while staying schemaless — the contract is still a
# plain Python method, no IDL/codegen. The ``$sandloom_type`` envelope of
# :mod:`sandloom.core.contract_wire` is applied to the value tree before packing, so
# msgpack only ever sees primitives/maps/lists and needs no custom extensions.
_CONTENT_TYPE = "application/msgpack"


# The MAC-covered caller context (tenant, issue time, audience), see the module doc.
_CTX_HEADER = "X-SANDLOOM-RPC-Context"


#: Hash of the provider's contract schema, sent on every successful response.
_CONTRACT_HASH_HEADER = "X-SANDLOOM-Contract-Hash"


#: Response: wire codec of ``result`` (absent = 1, the tagged tree).
_CODEC_HEADER = "X-SANDLOOM-RPC-Codec"


#: Request: ``"2;<contract hash>"`` — the caller can decode a codec-v2 result
#: of THAT contract version (the provider answers v2 only on a hash match).
_ACCEPT_CODEC_HEADER = "X-SANDLOOM-RPC-Accept-Codec"


#: Response binding (module doc): request nonce, echoed by the provider with
#: a MAC over (nonce, status, body digest).
_NONCE_HEADER = rpc_binding.NONCE_HEADER


_RESPONSE_MAC_HEADER = rpc_binding.MAC_HEADER


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _pack(payload: Any) -> bytes:
    packed: bytes = msgpack.packb(payload, use_bin_type=True)
    return packed


def _unpack(data: bytes) -> Any:
    return msgpack.unpackb(data, raw=False, strict_map_key=False)


class RpcRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    args: list[Any] = []
    keyword_args: dict[str, Any] = Field(default_factory=dict, alias="kwargs")
    #: Wire codec of ``args`` / ``kwargs`` (1 = tagged tree, 2 = typed).
    codec: int = contract_wire.CODEC_TAGGED


def _parse_codec(raw: str | None) -> int:
    if raw is None:
        return contract_wire.CODEC_TAGGED
    try:
        value = int(raw)
    except ValueError:
        return contract_wire.CODEC_TAGGED
    return (
        value if value in contract_wire.SUPPORTED_CODECS else contract_wire.CODEC_TAGGED
    )


#: Describe-document key carrying the provider's ACCESS_TOKEN_EXPIRE_MINUTES
#: (additive; compared by the caller's readiness check, see rpc.readiness).
ACCESS_LIFETIME_DESCRIBE_KEY = "access_token_expire_minutes"
