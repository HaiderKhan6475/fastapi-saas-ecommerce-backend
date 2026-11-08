import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.pool import NullPool

from app import deps, models
from app.config import Settings
from app.database import Base, create_database_engine, get_db
from app.observability import install_operational_handling
from app.routers import auth, carts, health, orders, payments, products, tenants

TEST_SECRET_KEY = "test-only-secret-key-not-for-deployment-123456"
TEST_PASSWORD = "Test-password-123!"


@pytest.fixture
def test_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        APP_NAME="Test API",
        ENVIRONMENT="test",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        SECRET_KEY=SecretStr(TEST_SECRET_KEY),
        JWT_ALGORITHM="HS256",
        ACCESS_TOKEN_EXPIRE_MINUTES=30,
        CORS_ORIGINS=["http://testserver"],
    )


@pytest.fixture
def db_engine(tmp_path: Path):
    database_path = tmp_path / "test.sqlite3"
    engine = create_database_engine(
        URL.create(
            "sqlite+aiosqlite",
            database=str(database_path),
        ).render_as_string(hide_password=False),
        poolclass=NullPool,
    )

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    yield engine
    asyncio.run(engine.dispose())


@pytest.fixture
def db_session(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture
def client(
    db_session: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
):
    @asynccontextmanager
    async def test_lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        yield

    test_app = FastAPI(lifespan=test_lifespan)
    test_app.include_router(auth.router)
    test_app.include_router(tenants.router)
    test_app.include_router(products.router)
    test_app.include_router(carts.router)
    test_app.include_router(orders.router)
    test_app.include_router(payments.router)
    test_app.include_router(health.router)
    install_operational_handling(
        test_app,
        request_timeout_seconds=test_settings.REQUEST_TIMEOUT_SECONDS,
    )
    monkeypatch.setattr(deps, "settings", test_settings)

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        async with db_session() as session:
            yield session

    test_app.dependency_overrides[get_db] = get_test_db

    with TestClient(test_app) as test_client:
        yield test_client

    test_app.dependency_overrides.clear()


@pytest.fixture
def tenant(db_session: async_sessionmaker[AsyncSession]) -> models.Tenant:
    async def create_tenant() -> models.Tenant:
        async with db_session() as session:
            value = models.Tenant(name="Example Tenant", slug="example-tenant")
            session.add(value)
            await session.commit()
            return value

    return asyncio.run(create_tenant())


@pytest.fixture
def other_tenant(db_session: async_sessionmaker[AsyncSession]) -> models.Tenant:
    async def create_tenant() -> models.Tenant:
        async with db_session() as session:
            value = models.Tenant(name="Other Tenant", slug="other-tenant")
            session.add(value)
            await session.commit()
            return value

    return asyncio.run(create_tenant())


@pytest.fixture
def usd_tenant(db_session: async_sessionmaker[AsyncSession]) -> models.Tenant:
    async def create_tenant() -> models.Tenant:
        async with db_session() as session:
            value = models.Tenant(
                name="US Dollar Tenant",
                slug="usd-tenant",
                default_currency="USD",
            )
            session.add(value)
            await session.commit()
            return value

    return asyncio.run(create_tenant())


def _create_user(
    db_session: async_sessionmaker[AsyncSession],
    tenant_id: str | None,
    email: str,
    role: str,
    *,
    is_active: bool = True,
) -> models.User:
    async def create() -> models.User:
        async with db_session() as session:
            user = models.User(
                email=email,
                hashed_password=deps.hash_password(TEST_PASSWORD),
                role=role,
                tenant_id=tenant_id,
                is_active=is_active,
            )
            session.add(user)
            await session.commit()
            return user

    return asyncio.run(create())


@pytest.fixture
def tenant_admin(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
) -> models.User:
    return _create_user(
        db_session,
        tenant.id,
        "admin@example.com",
        "TenantAdmin",
    )


@pytest.fixture
def customer(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
) -> models.User:
    return _create_user(
        db_session,
        tenant.id,
        "customer@example.com",
        "Customer",
    )


@pytest.fixture
def other_customer(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
) -> models.User:
    return _create_user(
        db_session,
        tenant.id,
        "other-customer@example.com",
        "Customer",
    )


@pytest.fixture
def inactive_customer(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
) -> models.User:
    return _create_user(
        db_session,
        tenant.id,
        "inactive-customer@example.com",
        "Customer",
        is_active=False,
    )


@pytest.fixture
def other_tenant_admin(
    db_session: async_sessionmaker[AsyncSession],
    other_tenant: models.Tenant,
) -> models.User:
    return _create_user(
        db_session,
        other_tenant.id,
        "other-admin@example.com",
        "TenantAdmin",
    )


@pytest.fixture
def other_tenant_customer(
    db_session: async_sessionmaker[AsyncSession],
    other_tenant: models.Tenant,
) -> models.User:
    return _create_user(
        db_session,
        other_tenant.id,
        "other-tenant-customer@example.com",
        "Customer",
    )


@pytest.fixture
def usd_tenant_admin(
    db_session: async_sessionmaker[AsyncSession],
    usd_tenant: models.Tenant,
) -> models.User:
    return _create_user(
        db_session,
        usd_tenant.id,
        "usd-admin@example.com",
        "TenantAdmin",
    )


@pytest.fixture
def inactive_user(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
) -> models.User:
    return _create_user(
        db_session,
        tenant.id,
        "inactive@example.com",
        "TenantAdmin",
        is_active=False,
    )


@pytest.fixture
def superadmin(db_session: async_sessionmaker[AsyncSession]) -> models.User:
    return _create_user(
        db_session,
        None,
        "superadmin@example.com",
        "SuperAdmin",
    )


@pytest.fixture
def auth_headers() -> Callable[[models.User], dict[str, str]]:
    def create_headers(user: models.User) -> dict[str, str]:
        token = deps.create_access_token(
            {
                "sub": user.email,
                "role": user.role,
                "tenant_id": user.tenant_id,
            }
        )
        return {"Authorization": f"Bearer {token}"}

    return create_headers


@pytest.fixture
def sample_product(
    db_session: async_sessionmaker[AsyncSession],
    tenant: models.Tenant,
) -> models.Product:
    async def create_product() -> models.Product:
        async with db_session() as session:
            value = models.Product(
                name="Sample Product",
                description="Test product",
                price=Decimal("19.99"),
                stock=7,
                tenant_id=tenant.id,
                currency=tenant.default_currency,
            )
            session.add(value)
            await session.commit()
            return value

    return asyncio.run(create_product())


@pytest.fixture
def other_tenant_product(
    db_session: async_sessionmaker[AsyncSession],
    other_tenant: models.Tenant,
) -> models.Product:
    async def create_product() -> models.Product:
        async with db_session() as session:
            value = models.Product(
                name="Private Product",
                description=None,
                price=Decimal("9.99"),
                stock=3,
                tenant_id=other_tenant.id,
                currency=other_tenant.default_currency,
            )
            session.add(value)
            await session.commit()
            return value

    return asyncio.run(create_product())


@pytest.fixture
def async_run() -> Callable[[Any], Any]:
    return asyncio.run
