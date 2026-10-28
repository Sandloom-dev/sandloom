"""Comprehensive tests for error handler functionality."""

import logging
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from sandloom.core.http.error_handlers import (
    Mask5xxMiddleware,
    generic_exception_handler,
    http_exception_handler,
    logger,
    validation_exception_handler,
)


class TestHttpExceptionHandler:
    """Test HTTP exception handler function."""

    def test_http_exception_handler_basic(self) -> None:
        """Test basic HTTP exception handling."""
        # Setup
        mock_request = Mock(spec=Request)
        exception = HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
        )

        # Execute
        response = http_exception_handler(mock_request, exception)

        # Verify
        assert isinstance(response, JSONResponse)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.body == b'{"detail":"Resource not found","code":null}'

    def test_http_exception_handler_with_code(self) -> None:
        """Test HTTP exception handler with custom error code."""
        # Setup
        mock_request = Mock(spec=Request)
        exception = HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Bad request"
        )
        # Add code attribute dynamically
        exception.code = "bad_request_error"

        # Execute
        response = http_exception_handler(mock_request, exception)

        # Verify
        assert isinstance(response, JSONResponse)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.body == b'{"detail":"Bad request","code":"bad_request_error"}'

    def test_http_exception_handler_different_status_codes(self) -> None:
        """Test HTTP exception handler with various status codes."""
        mock_request = Mock(spec=Request)

        test_cases = [
            (status.HTTP_401_UNAUTHORIZED, "Unauthorized"),
            (status.HTTP_403_FORBIDDEN, "Forbidden"),
            (status.HTTP_422_UNPROCESSABLE_CONTENT, "Validation failed"),
            (status.HTTP_500_INTERNAL_SERVER_ERROR, "Internal error"),
        ]

        for status_code, detail in test_cases:
            exception = HTTPException(status_code=status_code, detail=detail)
            response = http_exception_handler(mock_request, exception)

            assert response.status_code == status_code
            expected_body = f'{{"detail":"{detail}","code":null}}'.encode()
            assert response.body == expected_body


class TestValidationExceptionHandler:
    """Test validation exception handler function."""

    def test_validation_exception_handler_basic(self) -> None:
        """Test basic validation exception handling."""
        # Setup
        mock_request = Mock(spec=Request)

        # Create a mock validation error
        mock_validation_error = Mock(spec=RequestValidationError)
        mock_validation_error.errors.return_value = [
            {"loc": ["field1"], "msg": "field required", "type": "value_error.missing"}
        ]

        # Execute
        response = validation_exception_handler(mock_request, mock_validation_error)

        # Verify
        assert isinstance(response, JSONResponse)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

        # Check that errors() was called
        mock_validation_error.errors.assert_called_once()

    def test_validation_exception_handler_multiple_errors(self) -> None:
        """Test validation exception handler with multiple validation errors."""
        # Setup
        mock_request = Mock(spec=Request)
        mock_validation_error = Mock(spec=RequestValidationError)
        mock_validation_error.errors.return_value = [
            {"loc": ["email"], "msg": "field required", "type": "value_error.missing"},
            {
                "loc": ["password"],
                "msg": "ensure this value has at least 8 characters",
                "type": "value_error.any_str.min_length",
            },
        ]

        # Execute
        response = validation_exception_handler(mock_request, mock_validation_error)

        # Verify
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        # The response should contain both errors
        mock_validation_error.errors.assert_called_once()


class TestGenericExceptionHandler:
    """Test generic exception handler function."""

    def test_generic_exception_handler_basic(self) -> None:
        """Test basic generic exception handling."""
        # Setup
        mock_request = Mock(spec=Request)
        exception = Exception("Something went wrong")

        # Execute
        with patch.object(logger, "error") as mock_logger:
            response = generic_exception_handler(mock_request, exception)

        # Verify
        assert isinstance(response, JSONResponse)
        assert response.status_code == 500
        assert (
            response.body
            == b'{"detail":"Internal server error","code":"internal_error"}'
        )

        # Verify logging: type + sanitized message via %-args, never an
        # f-string (which bypassed redaction).
        mock_logger.assert_called_once_with(
            "Unhandled error: %s", "Exception: Something went wrong", exc_info=True
        )

    def test_generic_exception_handler_different_exceptions(self) -> None:
        """Test generic exception handler with different exception types."""
        mock_request = Mock(spec=Request)

        test_exceptions: list[Exception] = [
            ValueError("Invalid value"),
            KeyError("missing_key"),
            TypeError("Type error"),
            RuntimeError("Runtime error"),
        ]

        for exception in test_exceptions:
            with patch.object(logger, "error") as mock_logger:
                response = generic_exception_handler(mock_request, exception)

            assert response.status_code == 500
            assert (
                response.body
                == b'{"detail":"Internal server error","code":"internal_error"}'
            )

            # Verify logging was called with exception info
            mock_logger.assert_called_once()
            args, kwargs = mock_logger.call_args
            assert type(exception).__name__ in args[1]
            assert kwargs["exc_info"] is True


class TestMask5xxMiddleware:
    """Test the (pure ASGI) Mask5xxMiddleware class."""

    @staticmethod
    async def _run(app: object) -> list[dict[str, object]]:
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        middleware = Mask5xxMiddleware(app=app)
        await middleware({"type": "http", "method": "GET", "path": "/"}, receive, send)
        return sent

    @pytest.mark.asyncio
    async def test_middleware_successful_response(self) -> None:
        async def ok_app(scope: object, receive: object, send) -> None:  # type: ignore[no-untyped-def]
            await JSONResponse({"ok": True})(scope, receive, send)  # type: ignore[arg-type]

        sent = await self._run(ok_app)
        assert sent[0]["status"] == 200

    @pytest.mark.asyncio
    async def test_middleware_catches_exception(self) -> None:
        async def boom(scope: object, receive: object, send: object) -> None:
            raise Exception("Database connection failed")

        with patch.object(logger, "error") as mock_logger:
            sent = await self._run(boom)

        assert sent[0]["status"] == 500
        assert sent[1]["body"] == (
            b'{"detail":"Internal server error","code":"internal_error"}'
        )
        mock_logger.assert_called_once_with(
            "Middleware caught: %s",
            "Exception: Database connection failed",
            exc_info=True,
        )

    @pytest.mark.asyncio
    async def test_middleware_different_exceptions(self) -> None:
        for exception in (
            ValueError("Invalid input"),
            ConnectionError("Network failure"),
            KeyError("Missing configuration"),
            RuntimeError("Unexpected error"),
        ):

            async def boom(
                scope: object, receive: object, send: object, exc: Exception = exception
            ) -> None:
                raise exc

            with patch.object(logger, "error") as mock_logger:
                sent = await self._run(boom)
            assert sent[0]["status"] == 500
            args, kwargs = mock_logger.call_args
            assert type(exception).__name__ in args[1]
            assert kwargs["exc_info"] is True

    @pytest.mark.asyncio
    async def test_middleware_maps_http_status_protocol(self) -> None:
        class Denied(Exception):
            http_status = 403
            code = "access_denied"
            detail = "Forbidden"

        async def deny(scope: object, receive: object, send: object) -> None:
            raise Denied()

        sent = await self._run(deny)
        assert sent[0]["status"] == 403
        assert sent[1]["body"] == b'{"detail":"Forbidden","code":"access_denied"}'


class TestLoggerConfiguration:
    """Test logger configuration."""

    def test_logger_exists_and_configured(self) -> None:
        """Test that logger is properly configured."""
        assert logger is not None
        assert logger.name == "sandloom.core.errors"
        assert isinstance(logger, logging.Logger)

    def test_logger_can_log_messages(self) -> None:
        """Test that logger can log messages at different levels."""
        with (
            patch.object(logger, "error") as mock_error,
            patch.object(logger, "warning") as mock_warning,
            patch.object(logger, "info") as mock_info,
        ):
            logger.error("Test error message")
            logger.warning("Test warning message")
            logger.info("Test info message")

            mock_error.assert_called_once_with("Test error message")
            mock_warning.assert_called_once_with("Test warning message")
            mock_info.assert_called_once_with("Test info message")


class TestIntegration:
    """Integration tests for error handling components."""

    def test_all_handlers_return_json_response(self) -> None:
        """Test that all handlers return JSONResponse objects."""
        mock_request = Mock(spec=Request)

        # Test HTTP exception handler
        http_exc = HTTPException(status_code=404, detail="Not found")
        http_response = http_exception_handler(mock_request, http_exc)
        assert isinstance(http_response, JSONResponse)

        # Test validation exception handler
        validation_exc = Mock(spec=RequestValidationError)
        validation_exc.errors.return_value = []
        validation_response = validation_exception_handler(mock_request, validation_exc)
        assert isinstance(validation_response, JSONResponse)

        # Test generic exception handler
        with patch.object(logger, "error"):
            generic_exc = Exception("Error")
            generic_response = generic_exception_handler(mock_request, generic_exc)
            assert isinstance(generic_response, JSONResponse)

    def test_error_response_format_consistency(self) -> None:
        """Test that all error responses follow consistent format."""
        mock_request = Mock(spec=Request)

        # All responses should have 'detail' and 'code' fields
        responses: list[JSONResponse] = []

        # HTTP exception
        http_exc = HTTPException(status_code=400, detail="Bad request")
        responses.append(http_exception_handler(mock_request, http_exc))

        # Validation exception
        validation_exc = Mock(spec=RequestValidationError)
        validation_exc.errors.return_value = []
        responses.append(validation_exception_handler(mock_request, validation_exc))

        # Generic exception
        with patch.object(logger, "error"):
            generic_exc = Exception("Error")
            responses.append(generic_exception_handler(mock_request, generic_exc))

        # All should be JSONResponse and have consistent structure
        for response in responses:
            assert isinstance(response, JSONResponse)
            # Response body should be parseable JSON with detail and code fields
            assert b"detail" in response.body
            assert b"code" in response.body

    def test_module_imports_correctly(self) -> None:
        """Test that all module components can be imported correctly."""
        from sandloom.core.http.error_handlers import (
            Mask5xxMiddleware,
            generic_exception_handler,
            http_exception_handler,
            logger,
            validation_exception_handler,
        )

        # All should be callable or instances
        assert callable(http_exception_handler)
        assert callable(validation_exception_handler)
        assert callable(generic_exception_handler)
        assert isinstance(Mask5xxMiddleware, type)  # Should be a class
        assert isinstance(logger, logging.Logger)


class TestHttpExceptionFromMiddleware:
    """GLM review #3: an HTTPException raised ABOVE ExceptionMiddleware (in a
    middleware) must keep its status and the {"detail","code"} format instead
    of reaching ServerErrorMiddleware as a masked 500."""

    @staticmethod
    def _app(position: str):  # type: ignore[no-untyped-def]
        from fastapi import FastAPI
        from starlette.exceptions import HTTPException as StarletteHTTPException
        from starlette.middleware import Middleware

        from sandloom.core.http.error_handlers import register_error_handlers
        from sandloom.core.http.middlewares import register_middlewares

        class Raising:
            def __init__(self, app: object) -> None:
                self.app = app

            async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
                if scope["type"] == "http":
                    path = scope["path"]
                    if path == "/fastapi":
                        raise HTTPException(status_code=429, detail="slow down")
                    if path == "/starlette":
                        raise StarletteHTTPException(status_code=403, detail="no")
                await self.app(scope, receive, send)  # type: ignore[operator]

        app = FastAPI()
        register_error_handlers(app)
        register_middlewares(app)

        @app.get("/fastapi")
        @app.get("/starlette")
        async def ok() -> dict[str, bool]:
            return {"ok": True}

        if position == "outermost":
            app.add_middleware(Raising)
        else:  # inside the framework's 5xx mask (e.g. a framework middleware)
            idx = next(
                i
                for i, m in enumerate(app.user_middleware)
                if m.cls is Mask5xxMiddleware
            )
            app.user_middleware.insert(idx + 1, Middleware(Raising))
        return app

    @pytest.mark.parametrize("position", ["outermost", "inside_mask"])
    @pytest.mark.parametrize(
        ("path", "status_code", "detail"),
        [("/fastapi", 429, "slow down"), ("/starlette", 403, "no")],
    )
    def test_status_and_format_kept(
        self, position: str, path: str, status_code: int, detail: str
    ) -> None:
        from starlette.testclient import TestClient

        client = TestClient(self._app(position), raise_server_exceptions=False)
        response = client.get(path)
        assert response.status_code == status_code
        assert response.json() == {"detail": detail, "code": None}
