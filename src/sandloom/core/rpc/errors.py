"""Caller-facing RPC failures and the provider's internal context error."""

from __future__ import annotations

from fastapi import HTTPException, status


class _RpcContextError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class _RpcRefused(Exception):
    """The provider refused a request it could not authenticate.

    Its answer carries this request's nonce but no keyed binding (a provider
    keys only a response to a VERIFIED request), so nothing in it is
    authenticated: ``code`` is the provider's error code as a DIAGNOSTIC
    only. Never retried — the same request is refused again.
    """

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(f"provider refused the request ({status_code} {code})")
        self.status_code = status_code
        self.code = code


class RemoteServiceUnavailable(HTTPException):
    """A remote service binding exists, but the provider cannot serve the call.

    Transport / trust failures only (unreachable, timeout, a request or
    response that does not authenticate, provider crash) — never a domain
    error, which
    reaches the caller as the provider's own status. For ``reason="timeout"``
    the remote call MAY HAVE COMMITTED: retrying a decorated command with the
    same idempotency argument replays its result instead of re-executing it.
    ``code`` names the failure for programmatic handling.
    """

    def __init__(
        self,
        service_name: str,
        *,
        reason: str,
        code: str = "remote_service_unavailable",
    ) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Remote service {service_name!r} is unavailable: {reason}",
        )
        self.code = code


class RemoteServiceBadResponse(HTTPException):
    """A provider replied, but not with Sandloom's RPC envelope."""

    def __init__(self, service_name: str, *, reason: str | None = None) -> None:
        super().__init__(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Remote service {service_name!r} returned an invalid response"
                + (f": {reason}" if reason else "")
            ),
        )
