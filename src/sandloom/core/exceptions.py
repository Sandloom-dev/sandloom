"""Domain exceptions.

``status_code`` is the HTTP status the error maps to; ``error_code`` is a
stable machine-readable domain code for clients. They are deliberately
separate — one transport concern, one domain concern — so a 400 can carry
distinct domain codes and clients can branch on ``error_code`` without parsing
HTTP statuses.
"""


class AppException(Exception):
    status_code: int = 400
    error_code: str = "app_error"


class NotFound(AppException):
    status_code = 404
    error_code = "not_found"


class AuthError(AppException):
    status_code = 401
    error_code = "auth_error"


class Conflict(AppException):
    status_code = 409
    error_code = "conflict"


class RateLimitError(AppException):
    status_code = 429
    error_code = "rate_limited"


class DatabaseUnavailable(RuntimeError):
    """The database connection a request was using died (HTTP 503).

    Raised (``sandloom.core.sql_alchemy.dead_connection``) instead of the driver's
    disconnect error when a statement or COMMIT fails because its connection
    is dead: the outcome of the in-flight statement is unknown (an aborted
    transaction for anything before COMMIT; a COMMIT may or may not have
    applied), so the request is reported as a retryable 503 — never masked as
    a 500 and never silently re-executed. ``__cause__`` carries the original
    SQLAlchemy error; the connection is already invalidated (``orig`` is the
    driver error).
    """

    http_status = 503
    code = "database_unavailable"
    detail = "Database temporarily unavailable; retry the request"
    headers = {"Retry-After": "1"}  # noqa: RUF012 - read-only class default
    connection_invalidated = True

    def __init__(self, orig: BaseException | None = None) -> None:
        super().__init__(self.detail)
        self.orig = orig
