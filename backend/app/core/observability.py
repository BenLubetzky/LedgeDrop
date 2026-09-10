"""Structured logging, request-correlated log records, and error monitoring.

Stage 9 Package 6/7. ``configure_logging`` is called once at import of
``app.main``; ``configure_sentry`` is a no-op unless ``SENTRY_DSN`` is set.
Neither ever logs invoice content - only request metadata.
"""

from __future__ import annotations

import json
import logging
import sys

from app.core.config import settings
from app.core.request_context import get_request_id

_SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key"}


class RequestIdFilter(logging.Filter):
    """Attach the current request's correlation id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.request_id = get_request_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "environment": settings.environment,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if settings.debug else logging.INFO)


def _scrub_event(event: dict, _hint: dict) -> dict:
    """Drop request bodies / cookies and mask sensitive headers before send."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() in _SENSITIVE_HEADERS:
                    headers[name] = "[filtered]"
    return event


def configure_sentry() -> None:
    if settings.sentry_dsn is None:
        return
    try:
        import sentry_sdk
    except ModuleNotFoundError:  # pragma: no cover - sentry-sdk is a declared dep
        logging.getLogger("app").warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.environment,
        send_default_pii=False,
        traces_sample_rate=0.1,
        before_send=_scrub_event,
    )
