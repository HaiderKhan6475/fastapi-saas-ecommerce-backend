from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import Pool

from app.config import settings

DATABASE_ROOT = Path(__file__).resolve().parent.parent
SQLITE_BUSY_TIMEOUT_MS = 30_000
SQLITE_CONNECT_TIMEOUT_SECONDS = SQLITE_BUSY_TIMEOUT_MS / 1_000


def _resolve_sqlite_url(database_url: str) -> URL:
    url = make_url(database_url)
    if url.drivername != "sqlite+aiosqlite":
        raise ValueError("Only sqlite+aiosqlite database URLs are supported")

    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return url

    database_path = Path(database).expanduser()
    if not database_path.is_absolute():
        database_path = DATABASE_ROOT / database_path
    database_path = database_path.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return url.set(database=str(database_path))


def create_database_engine(
    database_url: str,
    *,
    poolclass: type[Pool] | None = None,
    **engine_options: Any,
) -> AsyncEngine:
    url = _resolve_sqlite_url(database_url)
    is_memory_database = url.database in {None, "", ":memory:"} or (
        url.database is not None
        and url.database.startswith("file:")
        and url.query.get("mode") == "memory"
    )
    options: dict[str, Any] = {
        "connect_args": {"timeout": SQLITE_CONNECT_TIMEOUT_SECONDS},
        "pool_pre_ping": True,
    }
    if not is_memory_database and poolclass is None:
        options.update(pool_size=5, max_overflow=5, pool_timeout=30)
    if poolclass is not None:
        options["poolclass"] = poolclass
    options.update(engine_options)

    database_engine = create_async_engine(url, **options)

    @event.listens_for(database_engine.sync_engine, "connect")
    def configure_sqlite_connection(dbapi_connection, _: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()

    @event.listens_for(database_engine.sync_engine, "first_connect")
    def configure_sqlite_journal_mode(dbapi_connection, _: object) -> None:
        if is_memory_database:
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    return database_engine


engine = create_database_engine(settings.DATABASE_URL)
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
