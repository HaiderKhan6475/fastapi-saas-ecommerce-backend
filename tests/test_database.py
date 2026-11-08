import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from app import models
from app.database import create_database_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
BASELINE_REVISION = "0001_initial_schema"


def _migration_config(database_path: Path) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    database_url = URL.create(
        "sqlite+aiosqlite",
        database=str(database_path),
    ).render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def test_engine_configures_sqlite_pragmas_and_creates_database_directory(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "created" / "nested" / "test.sqlite3"
    database_url = URL.create(
        "sqlite+aiosqlite",
        database=str(database_path),
    ).render_as_string(hide_password=False)
    engine = create_database_engine(database_url, poolclass=NullPool)

    async def verify() -> None:
        try:
            async with engine.connect() as connection:
                foreign_keys = await connection.exec_driver_sql("PRAGMA foreign_keys")
                busy_timeout = await connection.exec_driver_sql("PRAGMA busy_timeout")
                journal_mode = await connection.exec_driver_sql("PRAGMA journal_mode")

                assert foreign_keys.scalar_one() == 1
                assert busy_timeout.scalar_one() == 30_000
                assert journal_mode.scalar_one().lower() == "wal"
        finally:
            await engine.dispose()

    asyncio.run(verify())
    assert database_path.parent.is_dir()
    assert database_path.is_file()


def test_database_constraints_reject_invalid_products(
    db_engine,
    tenant,
) -> None:
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)

    async def insert_invalid_products() -> None:
        async with async_session() as session:
            with pytest.raises(IntegrityError):
                session.add(
                    models.Product(
                        name="Negative stock",
                        price=1.0,
                        stock=-1,
                        tenant_id=tenant.id,
                    )
                )
                await session.commit()
            await session.rollback()

            with pytest.raises(IntegrityError):
                session.add(
                    models.Product(
                        name="Invalid price",
                        price=0,
                        stock=0,
                        tenant_id=tenant.id,
                    )
                )
                await session.commit()

    asyncio.run(insert_invalid_products())


def test_sqlite_foreign_key_prevents_orphan_product(
    db_engine,
) -> None:
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)

    async def insert_orphan() -> None:
        async with async_session() as session:
            session.add(
                models.Product(
                    name="Orphan",
                    price=1.0,
                    stock=0,
                    tenant_id="missing-tenant",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    asyncio.run(insert_orphan())


def test_tenant_delete_cascades_to_users_and_products(
    db_engine,
    tenant,
    tenant_admin,
    sample_product,
) -> None:
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)

    async def delete_tenant() -> tuple[int, int]:
        async with async_session() as session:
            await session.execute(
                delete(models.Tenant).where(models.Tenant.id == tenant.id)
            )
            await session.commit()
        async with async_session() as session:
            user_count = await session.scalar(
                select(func.count()).select_from(models.User)
            )
            product_count = await session.scalar(
                select(func.count()).select_from(models.Product)
            )
            return int(user_count or 0), int(product_count or 0)

    assert tenant_admin.tenant_id == tenant.id
    assert sample_product.tenant_id == tenant.id
    assert asyncio.run(delete_tenant()) == (0, 0)


def test_tenant_product_index_is_used_for_scoped_listing(
    db_engine,
) -> None:
    async def inspect_query_plan() -> str:
        async with db_engine.connect() as connection:
            result = await connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN "
                "SELECT id FROM products WHERE tenant_id = ? ORDER BY id LIMIT ?",
                ("tenant", 10),
            )
            return " ".join(str(row) for row in result.all())

    plan = asyncio.run(inspect_query_plan())
    assert "ix_products_tenant_id_id" in plan


def test_migrations_create_current_schema_and_constraints(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    config = _migration_config(database_path)

    command.upgrade(config, "head")
    command.check(config)

    connection = sqlite3.connect(database_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "alembic_version",
            "tenants",
            "users",
            "products",
            "auth_rate_limit_buckets",
            "carts",
            "cart_items",
            "orders",
            "order_items",
        } <= tables

        product_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='products'"
        ).fetchone()[0]
        assert "ck_products_price_positive" in product_ddl
        assert "ck_products_stock_nonnegative" in product_ddl
        assert "ck_products_currency_format" in product_ddl
        assert "ck_products_version_positive" in product_ddl
        assert "NUMERIC(15, 4)" in product_ddl
        tenant_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tenants'"
        ).fetchone()[0]
        assert "default_currency" in tenant_ddl
        assert "ck_tenants_default_currency_format" in tenant_ddl

        product_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('products')")
        }
        user_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('users')")
        }
        assert "ix_products_tenant_id_id" in product_indexes
        assert "ix_products_tenant_currency_id" in product_indexes
        assert "ix_products_name" in product_indexes
        assert "ix_users_tenant_id" in user_indexes
        assert "ix_users_email" in user_indexes
        assert "uq_users_tenant_id_id" in user_indexes
        assert (
            next(
                row[2]
                for row in connection.execute("PRAGMA index_list('products')")
                if row[1] == "ix_products_tenant_id_id"
            )
            == 1
        )
        assert (
            next(
                row[2]
                for row in connection.execute("PRAGMA index_list('users')")
                if row[1] == "uq_users_tenant_id_id"
            )
            == 1
        )
        assert "trg_order_items_immutable_snapshots" in {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        rate_limit_indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list('auth_rate_limit_buckets')"
            )
        }
        assert "ix_auth_rate_limit_window_start" in rate_limit_indexes
    finally:
        connection.close()


def test_commerce_migration_preserves_existing_product_values(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "commerce-preservation.sqlite3"
    config = _migration_config(database_path)
    command.upgrade(config, "0004_product_currency_decimal")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO tenants "
            "(id, name, slug, created_at, default_currency) "
            "VALUES (?, ?, ?, ?, ?)",
            ("tenant-id", "Legacy Tenant", "legacy-tenant", "2025-01-01", "PKR"),
        )
        connection.execute(
            "INSERT INTO users "
            "(id, email, hashed_password, role, is_active, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, "legacy@example.com", "hash", "Customer", 1, "tenant-id"),
        )
        connection.execute(
            "INSERT INTO products "
            "(id, name, description, price, currency, stock, is_available, "
            "tenant_id, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "Legacy product", None, "4.2500", "USD", 2, 1, "tenant-id", 1),
        )
        connection.commit()
        original_product = connection.execute(
            "SELECT typeof(price), quote(price), currency FROM products WHERE id=1"
        ).fetchone()
    finally:
        connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT typeof(price), quote(price), currency FROM products WHERE id=1"
            ).fetchone()
            == original_product
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0006_payment_foundation"
        )

        product_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('products')")
        }
        user_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('users')")
        }
        assert "ix_products_tenant_id_id" in product_indexes
        assert "uq_users_tenant_id_id" in user_indexes
        assert (
            next(
                row[2]
                for row in connection.execute("PRAGMA index_list('products')")
                if row[1] == "ix_products_tenant_id_id"
            )
            == 1
        )
        assert (
            next(
                row[2]
                for row in connection.execute("PRAGMA index_list('users')")
                if row[1] == "uq_users_tenant_id_id"
            )
            == 1
        )
        assert (
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_order_items_immutable_snapshots'"
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def test_commerce_migration_refuses_downgrade_with_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "commerce-downgrade-data.sqlite3"
    config = _migration_config(database_path)
    command.upgrade(config, "head")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO tenants "
            "(id, name, slug, created_at, default_currency) "
            "VALUES (?, ?, ?, ?, ?)",
            ("tenant-id", "Tenant", "tenant", "2025-01-01", "PKR"),
        )
        connection.execute(
            "INSERT INTO users "
            "(id, email, hashed_password, role, is_active, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, "customer@example.com", "hash", "Customer", 1, "tenant-id"),
        )
        connection.execute(
            "INSERT INTO carts (tenant_id, customer_id) VALUES (?, ?)",
            ("tenant-id", 1),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="commerce data exists"):
        command.downgrade(config, "0004_product_currency_decimal")

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM carts").fetchone()[0] == 1
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0005_commerce_schema"
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_commerce_migration_downgrades_empty_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "commerce-downgrade-empty.sqlite3"
    config = _migration_config(database_path)
    command.upgrade(config, "head")
    command.downgrade(config, "0004_product_currency_decimal")

    connection = sqlite3.connect(database_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert not {"carts", "cart_items", "orders", "order_items"} & tables
        product_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('products')")
        }
        assert "ix_products_tenant_id_id" in product_indexes
        index_is_unique = connection.execute(
            "SELECT \"unique\" FROM pragma_index_list('products') "
            "WHERE name='ix_products_tenant_id_id'"
        ).fetchone()[0]
        assert index_is_unique == 0
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0004_product_currency_decimal"
        )
    finally:
        connection.close()


def test_payment_migration_refuses_downgrade_with_payment_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "payment-downgrade-data.sqlite3"
    config = _migration_config(database_path)
    command.upgrade(config, "head")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO tenants "
            "(id, name, slug, created_at, default_currency) "
            "VALUES (?, ?, ?, ?, ?)",
            ("payment-tenant", "Tenant", "payment-tenant", "2025-01-01", "PKR"),
        )
        connection.execute(
            "INSERT INTO tenant_payment_accounts "
            "(tenant_id, provider, merchant_account_reference, is_active) "
            "VALUES (?, ?, ?, ?)",
            ("payment-tenant", "test_gateway", "account-ref", 1),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="payment data exists"):
        command.downgrade(config, "0005_commerce_schema")

    connection = sqlite3.connect(database_path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM tenant_payment_accounts"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0006_payment_foundation"
        )
    finally:
        connection.close()


def test_payment_migration_downgrades_empty_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "payment-downgrade-empty.sqlite3"
    config = _migration_config(database_path)
    command.upgrade(config, "head")
    command.downgrade(config, "0005_commerce_schema")

    connection = sqlite3.connect(database_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert (
            not {
                "tenant_payment_accounts",
                "payments",
                "payment_webhook_events",
                "refunds",
            }
            & tables
        )
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0005_commerce_schema"
        )
    finally:
        connection.close()


def test_verified_legacy_baseline_upgrade_preserves_data_and_indexes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    config = _migration_config(database_path)

    command.upgrade(config, BASELINE_REVISION)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO tenants (id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            ("tenant-id", "Legacy Tenant", "legacy-tenant", "2025-01-01 00:00:00"),
        )
        connection.execute(
            "INSERT INTO users "
            "(id, email, hashed_password, role, is_active, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, "legacy@example.com", "hash", "TenantAdmin", 1, "tenant-id"),
        )
        connection.execute(
            "INSERT INTO products "
            "(id, name, description, price, stock, is_available, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Legacy product", None, 4.25, 2, 1, "tenant-id"),
        )
        connection.commit()
        connection.execute("DROP TABLE alembic_version")
        connection.commit()
    finally:
        connection.close()

    command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        assert connection.execute(
            "SELECT price, stock FROM products WHERE id=1"
        ).fetchone() == (4.25, 2)
        assert connection.execute(
            "SELECT default_currency FROM tenants WHERE id='tenant-id'"
        ).fetchone() == ("PKR",)
        assert connection.execute(
            "SELECT currency, version FROM products WHERE id=1"
        ).fetchone() == ("PKR", 1)

        product_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('products')")
        }
        assert {
            "ix_products_name",
            "ix_products_tenant_id_id",
            "ix_products_tenant_currency_id",
        } <= product_indexes
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO products "
                "(name, price, stock, is_available, tenant_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Invalid stock", 1.0, -1, 1, "tenant-id"),
            )
        connection.rollback()
    finally:
        connection.close()


def test_product_price_migration_stops_before_rounding_legacy_values(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "unsupported-price.sqlite3"
    config = _migration_config(database_path)

    command.upgrade(config, "0003_auth_hardening")
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO tenants (id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            ("tenant-id", "Legacy Tenant", "legacy-tenant", "2025-01-01 00:00:00"),
        )
        connection.execute(
            "INSERT INTO products "
            "(name, description, price, stock, is_available, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Precise product", None, 1.23456, 1, 1, "tenant-id"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="without changing its value"):
        command.upgrade(config, "head")

    connection = sqlite3.connect(database_path)
    try:
        product_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('products')")
        }
        assert "currency" not in product_columns
        assert "version" not in product_columns
        assert connection.execute("SELECT price FROM products").fetchone()[0] == 1.23456
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0003_auth_hardening"
        )
    finally:
        connection.close()
