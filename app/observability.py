import asyncio
import json
import logging
import re
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = b"x-request-id"
request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
application_logger = logging.getLogger("saas_ecommerce")
_SAFE_EVENTS = {
    "http_request_completed",
    "http_request_timed_out",
    "unhandled_application_exception",
    "database_readiness_failed",
}


class StructuredLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event_name = getattr(record, "event_name", None)
        document: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": (
                event_name
                if isinstance(event_name, str) and event_name in _SAFE_EVENTS
                else "application_event"
            ),
        }

        request_id = getattr(record, "request_id", None) or request_id_context.get()
        if request_id is not None:
            document["request_id"] = request_id

        for field in (
            "method",
            "route",
            "status_code",
            "duration_ms",
            "error_type",
        ):
            value = getattr(record, field, None)
            if isinstance(value, (str, int, float)):
                document[field] = value

        return json.dumps(document, separators=(",", ":"), ensure_ascii=True)


def configure_application_logging() -> None:
    application_logger.disabled = False
    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False
    logging.getLogger("uvicorn.access").disabled = True
    if not any(
        isinstance(handler, logging.StreamHandler)
        and isinstance(handler.formatter, StructuredLogFormatter)
        for handler in application_logger.handlers
    ):
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredLogFormatter())
        application_logger.addHandler(handler)


def _request_id(scope: Scope) -> str:
    for header_name, header_value in scope.get("headers", []):
        if header_name.lower() != REQUEST_ID_HEADER:
            continue
        if len(header_value) != 36:
            break
        try:
            candidate = header_value.decode("ascii")
            parsed = UUID(candidate)
        except (UnicodeDecodeError, ValueError):
            break
        if str(parsed) == candidate.lower():
            return str(parsed)
        break
    return str(uuid4())


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and len(path) <= 200:
        return path
    return "unmatched"


def _safe_method(scope: Scope) -> str:
    method = scope.get("method", "")
    if isinstance(method, str) and re.fullmatch(r"[A-Z]{1,16}", method):
        return method
    return "OTHER"


class OperationalMiddleware:
    def __init__(self, app: ASGIApp, request_timeout_seconds: float) -> None:
        self.app = app
        self.request_timeout_seconds = request_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id(scope)
        context_token = request_id_context.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started_at = time.perf_counter()
        response_started = False
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != REQUEST_ID_HEADER
                ]
                headers.append((REQUEST_ID_HEADER, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            try:
                async with asyncio.timeout(self.request_timeout_seconds):
                    await self.app(scope, receive, send_with_request_id)
            except TimeoutError as exc:
                if not response_started:
                    await self._send_error(
                        send_with_request_id,
                        status_code=504,
                        detail="Request timed out",
                    )
                application_logger.warning(
                    "request timed out",
                    extra={
                        "event_name": "http_request_timed_out",
                        "error_type": type(exc).__name__,
                    },
                )
            except Exception as exc:
                application_logger.error(
                    "application request failed",
                    extra={
                        "event_name": "unhandled_application_exception",
                        "error_type": type(exc).__name__,
                    },
                )
                if not response_started:
                    await self._send_error(
                        send_with_request_id,
                        status_code=500,
                        detail="Internal server error",
                    )
        finally:
            log_level = (
                logging.ERROR
                if status_code >= 500
                else logging.WARNING
                if status_code >= 400
                else logging.INFO
            )
            application_logger.log(
                log_level,
                "request completed",
                extra={
                    "event_name": "http_request_completed",
                    "method": _safe_method(scope),
                    "route": _route_template(scope),
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                },
            )
            request_id_context.reset(context_token)

    @staticmethod
    async def _send_error(
        send: Send,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def install_operational_handling(
    app: FastAPI,
    *,
    request_timeout_seconds: float,
) -> None:
    configure_application_logging()

    async def handle_http_exception(
        _: Request,
        exc: Exception,
    ) -> JSONResponse:
        if not isinstance(exc, StarletteHTTPException):
            raise exc
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    async def handle_validation_exception(
        _: Request,
        exc: Exception,
    ) -> JSONResponse:
        if not isinstance(exc, RequestValidationError):
            raise exc
        return JSONResponse(
            status_code=422,
            content={"detail": "Request validation failed"},
        )

    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_exception)
    app.add_middleware(
        OperationalMiddleware,
        request_timeout_seconds=request_timeout_seconds,
    )
