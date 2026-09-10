"""Per-request context (a correlation id) carried on a context variable.

Set once by :class:`app.core.middleware.RequestIDMiddleware` at the edge of the
request and read anywhere downstream - logging, error responses - without
threading it through call signatures.
"""

from __future__ import annotations

import contextvars

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ledgerdrop_request_id", default=None
)


def bind_request_id(request_id: str) -> contextvars.Token:
    """Bind ``request_id`` for the current context; returns a reset token."""
    return _request_id.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


def get_request_id() -> str | None:
    """The current request's correlation id, or ``None`` outside a request."""
    return _request_id.get()
