"""Resolve the real client IP of a request behind (zero or more) proxies.

The single source of truth for "who is the client?" wherever the framework keys
behaviour on the source address (rate limiting, idempotency fingerprints, audit
context). Every caller MUST go through :func:`get_client_ip` instead of reading
``X-Forwarded-For`` itself.

Policy:

* ``TRUST_PROXY_HEADERS`` off (default) — the immediate TCP peer is the client;
  forwarding headers are ignored entirely.
* ``TRUST_PROXY_HEADERS`` on + ``TRUSTED_PROXY_IPS`` (CIDRs) — forwarding headers
  are honoured only when the immediate peer is a trusted proxy. The
  ``X-Forwarded-For`` chain is then walked **right-to-left**, skipping every hop
  that is itself a trusted proxy, and the first *untrusted* hop is the client.
  The left part of the header is whatever the client sent, so taking the
  leftmost entry (the historical behaviour) let any client pick its own bucket.
* ``TRUST_PROXY_HEADERS`` on + empty ``TRUSTED_PROXY_IPS`` — in production we
  cannot tell a gateway from a spoofing client, so the peer is used (fail
  closed, one-time warning). Outside production the immediate peer is assumed to
  be the (single) proxy and the RIGHTMOST entry — the address that proxy saw —
  is used; never the client-controlled leftmost one.
"""

from __future__ import annotations

import ipaddress
import logging
from functools import lru_cache
from typing import Any

from sandloom.core.config.settings import settings
from sandloom.core.observability.metrics import http_untrusted_forwarded_headers_total

logger = logging.getLogger("sandloom.core.http.client_ip")

_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Guard so the "trusting XFF without an allowlist" warning is emitted once.
_warned_no_allowlist = False
_warned_untrusted_forwarding = False


@lru_cache(maxsize=32)
def _parse_networks(cidrs: tuple[str, ...]) -> tuple[_IPNetwork, ...]:
    nets: list[_IPNetwork] = []
    for cidr in cidrs:
        try:
            nets.append(ipaddress.ip_network(cidr.strip(), strict=False))
        except ValueError:  # pragma: no cover - rejected by settings validation
            logger.warning("Ignoring invalid TRUSTED_PROXY_IPS entry %r", cidr)
    return tuple(nets)


def _parse_ip(raw: str) -> _IPAddress | None:
    value = raw.strip().strip('"')
    if value.startswith("[") and "]" in value:  # "[2001:db8::1]:443"
        value = value[1 : value.index("]")]
    elif value.count(":") == 1 and "." in value:  # "203.0.113.7:51234"
        value = value.split(":", 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def is_trusted_proxy(ip: str, cidrs: list[str] | tuple[str, ...]) -> bool:
    """True if ``ip`` falls within any of the trusted-proxy ``cidrs``."""
    addr = _parse_ip(ip)
    if addr is None:
        return False
    return any(addr in net for net in _parse_networks(tuple(cidrs)))


def client_ip_from_parts(
    peer: str | None,
    forwarded_for: str | None,
    *,
    trust_proxy_headers: bool,
    trusted_proxy_ips: list[str] | tuple[str, ...],
    is_prod: bool,
) -> str:
    """Pure resolution core (no settings/request access) — see module doc."""
    global _warned_no_allowlist
    peer_ip = peer or "unknown"
    if not trust_proxy_headers or not forwarded_for:
        return peer_ip
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    if not hops:
        return peer_ip
    if not trusted_proxy_ips:
        if not _warned_no_allowlist:
            _warned_no_allowlist = True
            logger.warning(
                "TRUST_PROXY_HEADERS is on but TRUSTED_PROXY_IPS is empty; "
                "X-Forwarded-For cannot be verified. Set TRUSTED_PROXY_IPS to "
                "your gateway CIDR(s). %s",
                "Ignoring X-Forwarded-For (using peer IP) in production."
                if is_prod
                else "Using the rightmost X-Forwarded-For hop (dev only).",
            )
        if is_prod:
            return peer_ip
        return hops[-1]
    if not is_trusted_proxy(peer_ip, trusted_proxy_ips):
        # Direct client (not via a trusted proxy): its headers are untrusted.
        return peer_ip
    for hop in reversed(hops):
        if is_trusted_proxy(hop, trusted_proxy_ips):
            continue
        addr = _parse_ip(hop)
        return str(addr) if addr is not None else hop
    # Every hop is a trusted proxy: the leftmost is the closest we can get.
    addr = _parse_ip(hops[0])
    return str(addr) if addr is not None else hops[0]


def get_client_ip(request: Any, *, config: Any | None = None) -> str:
    """Return the real client IP for ``request`` (Starlette ``Request``-like).

    Only needs ``request.client.host`` and ``request.headers.get``, so it also
    works for lightweight stand-ins in tests. ``config`` overrides the settings
    object (defaults to the process settings).
    """
    global _warned_untrusted_forwarding
    cfg = config if config is not None else settings
    client = getattr(request, "client", None)
    peer = getattr(client, "host", None) if client is not None else None
    headers = getattr(request, "headers", None)
    forwarded = None
    if headers is not None:
        getlist = getattr(headers, "getlist", None)
        # Proxies may append another field line instead of merging the value.
        # Preserve their wire order before the right-to-left trust walk.
        forwarded = (
            ",".join(getlist("x-forwarded-for"))
            if callable(getlist)
            else headers.get("x-forwarded-for")
        )
    if forwarded and not getattr(cfg, "trust_proxy_headers", False):
        # Every occurrence counts (the warning is once): a sustained rate means
        # a proxy sits in front while client-IP policies key on its address.
        http_untrusted_forwarded_headers_total.inc()
        if not _warned_untrusted_forwarding:
            _warned_untrusted_forwarding = True
            logger.warning(
                "Forwarding headers received but TRUST_PROXY_HEADERS is off; "
                "using the direct peer for client-IP policies. Configure "
                "TRUSTED_PROXY_IPS and opt in if a trusted proxy is intended."
            )
    return client_ip_from_parts(
        peer,
        forwarded,
        trust_proxy_headers=bool(getattr(cfg, "trust_proxy_headers", False)),
        trusted_proxy_ips=list(getattr(cfg, "trusted_proxy_ips", []) or []),
        is_prod=bool(getattr(cfg, "is_prod", False)),
    )


__all__ = ["get_client_ip", "client_ip_from_parts", "is_trusted_proxy"]
