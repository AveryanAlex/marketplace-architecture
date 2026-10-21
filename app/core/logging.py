import json
import logging
import time
import traceback
from datetime import UTC, datetime
from uuid import uuid4

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.core.config import settings
from app.core.contract import reject_constant
from app.core.errors import error_response

SENSITIVE = {
    "password",
    "password_confirmation",
    "access_token",
    "refresh_token",
    "authorization",
    "jwt_secret",
    "token",
    "secret",
}


def redact(value):
    if isinstance(value, dict):
        return {k: "***" if k.lower() in SENSITIVE else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "event": record.getMessage(),
                "level": record.levelname.lower(),
                "timestamp": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
        )


def add_timestamp(logger, method_name, event):
    event.setdefault("timestamp", datetime.now(UTC).isoformat())
    return event


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            add_timestamp,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


class RequestLoggingMiddleware:
    """One completion event for every HTTP request, including validation/auth failures."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        clear_contextvars()
        request_id = str(uuid4())
        bind_contextvars(request_id=request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        start = time.perf_counter()
        timestamp = datetime.now(UTC).isoformat()
        status_code = 500
        started = False
        body = bytearray()
        body_log = None

        async def send_with_id(message):
            nonlocal status_code, started
            if message["type"] == "http.response.start":
                started = True
                status_code = message["status"]
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ]
                message["headers"].append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            # Buffer once and replay. Never consume a body without replaying it to FastAPI.
            more = True
            while more:
                message = await receive()
                if message["type"] == "http.disconnect":
                    status_code = 499
                    return
                body.extend(message.get("body", b""))
                if len(body) > settings.max_request_bytes:
                    response = error_response(
                        400,
                        "VALIDATION_ERROR",
                        "Request body too large",
                        {"fields": [{"field": "body", "message": "Body limit exceeded"}]},
                    )
                    await response(scope, receive, send_with_id)
                    return
                more = message.get("more_body", False)
            if scope["method"] in {"POST", "PUT", "DELETE"}:
                try:
                    body_log = (
                        redact(json.loads(body, parse_constant=reject_constant)) if body else None
                    )
                    if len(json.dumps(body_log)) > settings.log_body_limit:
                        body_log = "<body omitted: log limit exceeded>"
                except (ValueError, UnicodeDecodeError, RecursionError):
                    body_log = "<invalid JSON>"
            replayed = False

            async def replay():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, replay, send_with_id)
        except Exception as exc:
            structlog.get_logger().error(
                "request_failed",
                exception_type=type(exc).__name__,
                stack=traceback.format_tb(exc.__traceback__),
            )
            if started:
                raise
            response = error_response(500, "INTERNAL_ERROR", "Internal server error")
            await response(scope, receive, send_with_id)
        finally:
            event = dict(
                method=scope["method"],
                endpoint=scope["path"],
                status_code=status_code,
                duration_ms=round((time.perf_counter() - start) * 1000, 3),
                user_id=scope["state"].get("user_id"),
                timestamp=timestamp,
            )
            if scope["method"] in {"POST", "PUT", "DELETE"}:
                event["request_body"] = body_log
            structlog.get_logger().info("api_request", **event)
            clear_contextvars()
