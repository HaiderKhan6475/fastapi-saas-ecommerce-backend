import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Generator
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.observability import (
    StructuredLogFormatter,
    application_logger,
    configure_application_logging,
    install_operational_handling,
)

REQUEST_ID = "c765da6b-54fb-4556-b49b-6e24151ba7b6"
PASSWORD_SENTINEL = "request-password-secret"
TOKEN_SENTINEL = "authorization-token-secret"
EMAIL_SENTINEL = "private-user@example.com"
DATABASE_SENTINEL = "sqlite-database-secret"


@pytest.fixture
def structured_logs() -> Generator[list[str], None, None]:
    configure_application_logging()
    records: list[str] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    handler = CaptureHandler()
    handler.setFormatter(StructuredLogFormatter())
    application_logger.addHandler(handler)
    try:
        yield records
    finally:
        application_logger.removeHandler(handler)


def _observability_app(*, timeout_seconds: float = 30) -> FastAPI:
    app = FastAPI()
    install_operational_handling(app, request_timeout_seconds=timeout_seconds)
    return app


def test_logging_configuration_disables_raw_uvicorn_access_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access_logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access_logger, "disabled", False)

    configure_application_logging()

    assert access_logger.disabled


def test_health_endpoints_report_liveness_and_database_readiness(client) -> None:
    live = client.get("/health/live")
    ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert UUID(live.headers["x-request-id"])
    assert UUID(ready.headers["x-request-id"])


def test_readiness_returns_safe_503_and_cleans_up_session(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSession:
        closed = False

        async def execute(self, *_args: object, **_kwargs: object) -> None:
            raise OperationalError(
                "SELECT 1",
                {},
                RuntimeError(DATABASE_SENTINEL),
            )

    session = FailingSession()

    async def unavailable_db() -> AsyncGenerator[AsyncSession, None]:
        try:
            yield cast(AsyncSession, session)
        finally:
            session.closed = True

    monkeypatch.setitem(client.app.dependency_overrides, get_db, unavailable_db)

    response = client.get("/health/ready", headers={"X-Request-ID": REQUEST_ID})

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert response.headers["x-request-id"] == REQUEST_ID
    assert session.closed
    assert DATABASE_SENTINEL not in response.text


def test_validation_errors_use_safe_consistent_response() -> None:
    app = _observability_app()

    @app.get("/test/validation")
    async def validation(secret: int) -> int:
        return secret

    with TestClient(app) as test_client:
        response = test_client.get(
            f"/test/validation?secret={PASSWORD_SENTINEL}",
            headers={"X-Request-ID": REQUEST_ID},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "Request validation failed"}
    assert response.headers["x-request-id"] == REQUEST_ID
    assert PASSWORD_SENTINEL not in response.text


def test_request_logs_include_correlation_but_not_sensitive_values(
    client,
    structured_logs: list[str],
) -> None:
    response = client.post(
        f"/auth/login?email={EMAIL_SENTINEL}&token={TOKEN_SENTINEL}",
        data={"username": EMAIL_SENTINEL, "password": PASSWORD_SENTINEL},
        headers={
            "Authorization": f"Bearer {TOKEN_SENTINEL}",
            "X-Request-ID": REQUEST_ID,
        },
    )

    assert response.status_code == 401
    entry = next(
        json.loads(line)
        for line in structured_logs
        if json.loads(line)["event"] == "http_request_completed"
    )
    assert entry["request_id"] == REQUEST_ID
    assert entry["route"] == "/auth/login"
    serialized = json.dumps(entry)
    for sensitive_value in (
        PASSWORD_SENTINEL,
        TOKEN_SENTINEL,
        EMAIL_SENTINEL,
    ):
        assert sensitive_value not in serialized


def test_unexpected_exceptions_return_safe_500_and_redacted_log(
    structured_logs: list[str],
) -> None:
    app = _observability_app()

    @app.get("/test/failure")
    async def fail() -> None:
        raise RuntimeError(
            f"password={PASSWORD_SENTINEL} token={TOKEN_SENTINEL} email={EMAIL_SENTINEL}"
        )

    with TestClient(app) as test_client:
        response = test_client.get(
            "/test/failure",
            headers={"X-Request-ID": REQUEST_ID},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert response.headers["x-request-id"] == REQUEST_ID
    logged = "\n".join(structured_logs)
    assert '"error_type":"RuntimeError"' in logged
    assert REQUEST_ID in logged
    for sensitive_value in (
        PASSWORD_SENTINEL,
        TOKEN_SENTINEL,
        EMAIL_SENTINEL,
    ):
        assert sensitive_value not in logged


def test_unexpected_error_responses_include_cors_headers() -> None:
    app = _observability_app()

    @app.get("/test/failure")
    async def fail() -> None:
        raise RuntimeError("internal detail")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://client.example"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    with TestClient(app) as test_client:
        response = test_client.get(
            "/test/failure",
            headers={"Origin": "https://client.example"},
        )

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "https://client.example"
    assert response.json() == {"detail": "Internal server error"}


def test_timeout_responses_include_cors_headers() -> None:
    app = _observability_app(timeout_seconds=0.01)

    @app.get("/test/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(0.1)
        return {"status": "late"}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://client.example"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    with TestClient(app) as test_client:
        response = test_client.get(
            "/test/slow",
            headers={"Origin": "https://client.example"},
        )

    assert response.status_code == 504
    assert response.headers["access-control-allow-origin"] == "https://client.example"
    assert response.json() == {"detail": "Request timed out"}


def test_request_timeout_returns_gateway_timeout_with_correlation_id() -> None:
    app = _observability_app(timeout_seconds=0.01)

    @app.get("/test/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(0.1)
        return {"status": "late"}

    with TestClient(app) as test_client:
        response = test_client.get(
            "/test/slow",
            headers={"X-Request-ID": REQUEST_ID},
        )

    assert response.status_code == 504
    assert response.json() == {"detail": "Request timed out"}
    assert response.headers["x-request-id"] == REQUEST_ID


def test_engine_is_disposed_when_lifespan_exits_with_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    disposed = False

    class EngineStub:
        async def dispose(self) -> None:
            nonlocal disposed
            disposed = True

    monkeypatch.setattr(main, "engine", EngineStub())

    async def run_lifespan() -> None:
        with pytest.raises(RuntimeError, match="shutdown test"):
            async with main.lifespan(main.app):
                raise RuntimeError("shutdown test")

    asyncio.run(run_lifespan())
    assert disposed
