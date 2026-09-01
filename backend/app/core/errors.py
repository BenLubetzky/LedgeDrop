"""Consistent API error handling.

Every error response - whether raised deliberately, produced by request
validation, or entirely unexpected - is serialised as::

    {"error": {"code": "SNAKE_UPPER_CODE", "message": "...", "details": [...]}}

``details`` is optional and only present when it carries useful structured
information (for example per-field validation errors). Internal exception text is
never exposed; unexpected errors always collapse to a generic 500.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")


class APIError(Exception):
    """Base class for errors that map to a well-formed API response.

    Application code should raise one of the concrete subclasses below rather
    than constructing generic ``HTTPException`` instances, so every failure
    shares one response shape.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: list[Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details
        super().__init__(self.message)

    def to_response(self) -> JSONResponse:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return JSONResponse(status_code=self.status_code, content={"error": body})


class BadRequestError(APIError):
    """Raised when a request is invalid."""
    status_code = status.HTTP_400_BAD_REQUEST
    code = "BAD_REQUEST"
    message = "The request could not be processed."


class NotFoundError(APIError):
    """Raised when a requested resource cannot be found."""
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"
    message = "The requested resource was not found."


class ConflictError(APIError):
    """Raised when a request conflicts with the current resource state."""
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"
    message = "The request conflicts with the current state of the resource."


class UnsupportedMediaTypeError(APIError):
    """Raised when an uploaded file type is not supported."""
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "UNSUPPORTED_MEDIA_TYPE"
    message = "The uploaded file type is not supported."


class PayloadTooLargeError(APIError):
    """Raised when an uploaded file exceeds the size limit."""
    status_code = 413
    code = "PAYLOAD_TOO_LARGE"
    message = "The uploaded file is too large."


class UnprocessableEntityError(APIError):
    """Raised when a valid request cannot be processed."""
    status_code = 422
    code = "UNPROCESSABLE_ENTITY"
    message = "The request was well-formed but could not be processed."


def _error_response(
    status_code: int, code: str, message: str, details: list[Any] | None = None
) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return JSONResponse(status_code=status_code, content={"error": body})


async def _handle_api_error(_: Request, exc: APIError) -> JSONResponse:
    """In case of api error refer to to_response"""
    return exc.to_response()


async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """In case of validation error."""
    details = [
        {
            "location": list(err.get("loc", [])),
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]
    return _error_response(
        422,  # "Unprocessable Content"
        "VALIDATION_ERROR",
        "One or more request parameters were invalid.",
        details,
    )


async def _handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """In case of http error."""
    # Starlette raises these for routing-level problems (404 on an unknown path,
    # 405, etc.) and for any HTTPException still raised by third-party code.
    code = {
        status.HTTP_404_NOT_FOUND: "NOT_FOUND",
        status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
        status.HTTP_401_UNAUTHORIZED: "UNAUTHORIZED",
        status.HTTP_403_FORBIDDEN: "FORBIDDEN",
    }.get(exc.status_code, "HTTP_ERROR")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return _error_response(exc.status_code, code, message)


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """In case of unexpected error."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "INTERNAL_ERROR",
        "An unexpected error occurred.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Add exception handlers for more informative error responses"""
    app.add_exception_handler(APIError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected_error)
