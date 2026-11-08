from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import deps, models


def test_tenant_registration_creates_tenant_and_admin(
    client: TestClient,
    db_session: async_sessionmaker[AsyncSession],
    async_run: Callable[[Any], Any],
) -> None:
    response = client.post(
        "/tenants/",
        json={
            "name": "New Tenant",
            "slug": "new-tenant",
            "admin_email": "owner@example.com",
            "admin_password": "Strong-password-123!",
        },
    )

    assert response.status_code == 201
    tenant_data = response.json()
    assert tenant_data["name"] == "New Tenant"
    assert tenant_data["slug"] == "new-tenant"
    assert tenant_data["default_currency"] == "PKR"
    assert tenant_data["id"]
    assert tenant_data["created_at"]

    async def load_admin() -> models.User | None:
        async with db_session() as session:
            result = await session.execute(
                select(models.User).where(models.User.email == "owner@example.com")
            )
            return result.scalar_one_or_none()

    admin = async_run(load_admin())
    assert admin is not None
    assert admin.role == "TenantAdmin"
    assert admin.tenant_id == tenant_data["id"]
    assert admin.hashed_password != "Strong-password-123!"
    assert admin.hashed_password.startswith("$argon2id$")
    assert deps.verify_password("Strong-password-123!", admin.hashed_password)


def test_tenant_registration_accepts_valid_currency_and_rejects_invalid_currency(
    client: TestClient,
) -> None:
    valid = client.post(
        "/tenants/",
        json={
            "name": "Dollar Tenant",
            "slug": "dollar-tenant",
            "admin_email": "dollar@example.com",
            "admin_password": "Strong-password-123!",
            "default_currency": "usd",
        },
    )
    invalid = client.post(
        "/tenants/",
        json={
            "name": "Invalid Currency Tenant",
            "slug": "invalid-currency-tenant",
            "admin_email": "invalid-currency@example.com",
            "admin_password": "Strong-password-123!",
            "default_currency": "ZZZ",
        },
    )

    assert valid.status_code == 201
    assert valid.json()["default_currency"] == "USD"
    assert invalid.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "Bad Email",
            "slug": "bad-email",
            "admin_email": "not-an-email",
            "admin_password": "Strong-password-123!",
        },
        {
            "name": "Short Password",
            "slug": "short-password",
            "admin_email": "owner@example.com",
            "admin_password": "short",
        },
        {
            "name": "Password Below Policy",
            "slug": "short-policy-password",
            "admin_email": "owner@example.com",
            "admin_password": "12345678901",
        },
        {
            "name": "Missing Fields",
            "slug": "missing-fields",
            "admin_email": "owner@example.com",
        },
    ],
)
def test_tenant_registration_rejects_invalid_input(
    client: TestClient,
    payload: dict[str, str],
) -> None:
    response = client.post("/tenants/", json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "second_registration",
    [
        {
            "name": "Another Tenant",
            "slug": "first-tenant",
            "admin_email": "another@example.com",
            "admin_password": "Strong-password-123!",
        },
        {
            "name": "Another Tenant",
            "slug": "another-tenant",
            "admin_email": "first@example.com",
            "admin_password": "Strong-password-123!",
        },
    ],
)
def test_tenant_registration_rejects_duplicate_slug_or_email(
    client: TestClient,
    second_registration: dict[str, str],
) -> None:
    first_response = client.post(
        "/tenants/",
        json={
            "name": "First Tenant",
            "slug": "first-tenant",
            "admin_email": "first@example.com",
            "admin_password": "Strong-password-123!",
        },
    )
    assert first_response.status_code == 201

    duplicate_response = client.post("/tenants/", json=second_registration)

    assert duplicate_response.status_code == 409


def test_login_returns_bearer_token_for_valid_credentials(
    client: TestClient,
    tenant_admin: models.User,
) -> None:
    response = client.post(
        "/auth/login",
        data={"username": tenant_admin.email, "password": "Test-password-123!"},
    )

    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert response.json()["access_token"]


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("admin@example.com", "incorrect-password"),
        ("missing@example.com", "Test-password-123!"),
    ],
)
def test_login_rejects_invalid_credentials(
    client: TestClient,
    username: str,
    password: str,
) -> None:
    response = client.post(
        "/auth/login",
        data={"username": username, "password": password},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "Invalid email or password"


def test_login_rejects_inactive_user(
    client: TestClient,
    inactive_user: models.User,
) -> None:
    response = client.post(
        "/auth/login",
        data={"username": inactive_user.email, "password": "Test-password-123!"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "token_kind",
    [
        "expired",
        "invalid_signature",
        "wrong_algorithm",
        "missing_expiry",
        "missing_subject",
    ],
)
def test_protected_endpoint_rejects_invalid_tokens(
    client: TestClient,
    tenant_admin: models.User,
    token_kind: str,
) -> None:
    claims: dict[str, object] = (
        {} if token_kind == "missing_subject" else {"sub": tenant_admin.email}
    )
    if token_kind != "missing_expiry":
        claims["exp"] = (
            datetime.now(UTC) - timedelta(seconds=1)
            if token_kind == "expired"
            else datetime.now(UTC) + timedelta(minutes=5)
        )
    if token_kind == "wrong_algorithm":
        token = jwt.encode(
            claims,
            "wrong-algorithm-test-key-" + "x" * 64,
            algorithm="HS512",
        )
    elif token_kind in {"missing_expiry", "missing_subject"}:
        token = jwt.encode(
            claims,
            deps.settings.SECRET_KEY.get_secret_value(),
            algorithm=deps.settings.JWT_ALGORITHM,
        )
    else:
        token = jwt.encode(
            claims,
            (
                "wrong-test-signing-key-" + "x" * 32
                if token_kind == "invalid_signature"
                else deps.settings.SECRET_KEY.get_secret_value()
            ),
            algorithm=deps.settings.JWT_ALGORITHM,
        )

    response = client.get(
        "/products/",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_protected_endpoint_rejects_token_for_deactivated_user(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    inactive_user: models.User,
) -> None:
    response = client.get(
        "/products/",
        headers=auth_headers(inactive_user),
    )

    assert response.status_code == 401


def test_deactivation_endpoint_requires_authentication(
    client: TestClient,
) -> None:
    response = client.post("/auth/users/1/deactivate")

    assert response.status_code == 401


def test_superadmin_can_deactivate_user_and_revoke_existing_tokens(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    superadmin: models.User,
    tenant_admin: models.User,
) -> None:
    deactivation = client.post(
        f"/auth/users/{tenant_admin.id}/deactivate",
        headers=auth_headers(superadmin),
    )

    assert deactivation.status_code == 200
    assert deactivation.json()["is_active"] is False
    rejected = client.get(
        "/products/",
        headers=auth_headers(tenant_admin),
    )
    assert rejected.status_code == 401


def test_non_superadmin_cannot_deactivate_users(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    customer: models.User,
) -> None:
    response = client.post(
        f"/auth/users/{customer.id}/deactivate",
        headers=auth_headers(tenant_admin),
    )

    assert response.status_code == 403


def test_login_rate_limit_is_configurable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps.settings, "LOGIN_RATE_LIMIT_PER_WINDOW", 2)
    responses = [
        client.post(
            "/auth/login",
            data={"username": "missing@example.com", "password": "wrong-password"},
        )
        for _ in range(3)
    ]

    assert [response.status_code for response in responses] == [401, 401, 429]
    assert responses[-1].headers["retry-after"]


def test_registration_rate_limit_is_configurable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps.settings, "REGISTRATION_RATE_LIMIT_PER_WINDOW", 2)

    def register(index: int):
        return client.post(
            "/tenants/",
            json={
                "name": f"Rate Limited Tenant {index}",
                "slug": f"rate-limited-{index}",
                "admin_email": f"rate-limited-{index}@example.com",
                "admin_password": "Strong-password-123!",
            },
        )

    responses = [register(index) for index in range(3)]

    assert [response.status_code for response in responses] == [201, 201, 429]


def test_passwords_longer_than_72_bytes_are_not_truncated(
    client: TestClient,
) -> None:
    password = "A" * 100 + "Strong-password-123!"
    registration = client.post(
        "/tenants/",
        json={
            "name": "Long Password Tenant",
            "slug": "long-password-tenant",
            "admin_email": "long-password@example.com",
            "admin_password": password,
        },
    )
    login = client.post(
        "/auth/login",
        data={"username": "long-password@example.com", "password": password},
    )

    assert registration.status_code == 201
    assert login.status_code == 200


def test_signed_role_and_tenant_claims_do_not_override_database(
    client: TestClient,
    customer: models.User,
    sample_product: models.Product,
    other_tenant_product: models.Product,
) -> None:
    token = deps.create_access_token(
        {
            "sub": customer.email,
            "role": "TenantAdmin",
            "tenant_id": other_tenant_product.tenant_id,
        }
    )
    headers = {"Authorization": f"Bearer {token}"}

    create_response = client.post(
        "/products/",
        headers=headers,
        json={"name": "Unauthorized", "price": 1, "stock": 1},
    )
    list_response = client.get("/products/", headers=headers)

    assert create_response.status_code == 403
    assert list_response.status_code == 200
    listed_ids = {item["id"] for item in list_response.json()}
    assert sample_product.id in listed_ids
    assert other_tenant_product.id not in listed_ids


def test_other_tenant_admin_cannot_list_or_delete_foreign_products(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    other_tenant_admin: models.User,
    sample_product: models.Product,
) -> None:
    headers = auth_headers(other_tenant_admin)
    listing = client.get("/products/", headers=headers)
    deletion = client.delete(f"/products/{sample_product.id}", headers=headers)

    assert listing.status_code == 200
    assert listing.json() == []
    assert deletion.status_code == 404


def test_create_product_requires_tenant_admin_and_returns_product(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
) -> None:
    response = client.post(
        "/products/",
        headers=auth_headers(tenant_admin),
        json={
            "name": "Desk Lamp",
            "description": "Adjustable desk lamp",
            "price": 32.5,
            "stock": 4,
        },
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Desk Lamp"
    assert Decimal(response.json()["price"]) == Decimal("32.5")
    assert response.json()["currency"] == "PKR"
    assert response.json()["stock"] == 4
    assert response.json()["version"] == 1
    assert response.json()["tenant_id"] == tenant_admin.tenant_id


def test_product_currency_defaults_are_isolated_per_tenant(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    usd_tenant_admin: models.User,
) -> None:
    pkr_product = client.post(
        "/products/",
        headers=auth_headers(tenant_admin),
        json={"name": "PKR item", "price": "120.50"},
    )
    usd_product = client.post(
        "/products/",
        headers=auth_headers(usd_tenant_admin),
        json={"name": "USD item", "price": "12.50"},
    )
    pkr_listing = client.get(
        "/products/",
        headers=auth_headers(tenant_admin),
        params={"currency": "USD"},
    )

    assert pkr_product.status_code == 201
    assert pkr_product.json()["currency"] == "PKR"
    assert usd_product.status_code == 201
    assert usd_product.json()["currency"] == "USD"
    assert pkr_listing.status_code == 200
    assert pkr_listing.json() == []


def test_product_creation_accepts_explicit_currency(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
) -> None:
    response = client.post(
        "/products/",
        headers=auth_headers(tenant_admin),
        json={"name": "Dollar priced", "price": "10.25", "currency": "usd"},
    )

    assert response.status_code == 201
    assert response.json()["currency"] == "USD"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Invalid Price", "price": 0, "stock": 1},
        {"name": "Negative Price", "price": -1, "stock": 1},
        {"name": "Negative Stock", "price": 1, "stock": -1},
        {"name": "  ", "price": 1, "stock": 0},
        {"name": "   ", "price": 1, "stock": 0},
        {"name": "Invalid Currency", "price": 1, "currency": "ZZZ"},
        {"name": "Bad Currency Format", "price": 1, "currency": "US"},
        {"name": "Client Tenant", "price": 1, "tenant_id": "foreign"},
        {"name": "Long Description", "price": 1, "description": "x" * 5001},
        {"name": "Too Precise", "price": "1.1234567890123456789"},
        {"price": 1, "stock": 1},
    ],
)
def test_create_product_rejects_invalid_input(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    payload: dict[str, Any],
) -> None:
    response = client.post(
        "/products/",
        headers=auth_headers(tenant_admin),
        json=payload,
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/products/", {"name": "Product", "price": 1, "stock": 0}),
        ("get", "/products/", None),
        ("patch", "/products/1", {"expected_version": 1, "name": "Updated"}),
        ("delete", "/products/1", None),
    ],
)
def test_product_endpoints_require_authentication(
    client: TestClient,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> None:
    response = client.request(method, path, json=payload)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/products/", {"name": "Product", "price": 1, "stock": 0}),
        ("delete", "/products/1", None),
        ("patch", "/products/1", {"expected_version": 1, "name": "Updated"}),
    ],
)
def test_customer_cannot_create_or_delete_products(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    customer: models.User,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> None:
    response = client.request(
        method,
        path,
        headers=auth_headers(customer),
        json=payload,
    )

    assert response.status_code == 403


def test_list_products_returns_only_current_tenant_products(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    sample_product: models.Product,
    other_tenant_product: models.Product,
) -> None:
    response = client.get(
        "/products/",
        headers=auth_headers(tenant_admin),
    )

    assert response.status_code == 200
    assert [product["id"] for product in response.json()] == [sample_product.id]
    assert response.json()[0]["tenant_id"] == tenant_admin.tenant_id
    assert other_tenant_product.id not in {product["id"] for product in response.json()}


def test_product_detail_is_tenant_scoped(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    other_tenant_admin: models.User,
    sample_product: models.Product,
) -> None:
    own_product = client.get(
        f"/products/{sample_product.id}",
        headers=auth_headers(tenant_admin),
    )
    foreign_product = client.get(
        f"/products/{sample_product.id}",
        headers=auth_headers(other_tenant_admin),
    )

    assert own_product.status_code == 200
    assert own_product.json()["id"] == sample_product.id
    assert Decimal(own_product.json()["price"]) == Decimal("19.99")
    assert own_product.json()["currency"] == "PKR"
    assert foreign_product.status_code == 404


def test_product_partial_update_and_null_description(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    sample_product: models.Product,
) -> None:
    response = client.patch(
        f"/products/{sample_product.id}",
        headers=auth_headers(tenant_admin),
        json={
            "expected_version": 1,
            "price": "22.75",
            "description": None,
        },
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["name"] == "Sample Product"
    assert updated["price"] == "22.75"
    assert updated["currency"] == "PKR"
    assert updated["description"] is None
    assert updated["stock"] == 7
    assert updated["version"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_version": 1},
        {"expected_version": 1, "name": None},
        {"expected_version": 1, "price": None},
        {"expected_version": 1, "currency": None},
        {"expected_version": 1, "stock": None},
        {"expected_version": 1, "is_available": None},
        {"expected_version": 1, "price": 0},
        {"expected_version": 1, "stock": -1},
        {"expected_version": 1, "currency": "ZZZ"},
        {"expected_version": 1, "tenant_id": "foreign", "name": "Spoof"},
    ],
)
def test_product_update_rejects_invalid_partial_data(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    sample_product: models.Product,
    payload: dict[str, Any],
) -> None:
    response = client.patch(
        f"/products/{sample_product.id}",
        headers=auth_headers(tenant_admin),
        json=payload,
    )

    assert response.status_code == 422


def test_product_update_detects_stale_version_and_foreign_tenant(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    other_tenant_admin: models.User,
    sample_product: models.Product,
) -> None:
    headers = auth_headers(tenant_admin)
    initial = client.patch(
        f"/products/{sample_product.id}",
        headers=headers,
        json={"expected_version": 1, "name": "Renamed"},
    )
    stale = client.patch(
        f"/products/{sample_product.id}",
        headers=headers,
        json={"expected_version": 1, "name": "Stale update"},
    )
    foreign = client.patch(
        f"/products/{sample_product.id}",
        headers=auth_headers(other_tenant_admin),
        json={"expected_version": 2, "name": "Cross-tenant"},
    )

    assert initial.status_code == 200
    assert initial.json()["version"] == 2
    assert stale.status_code == 409
    assert (
        stale.json()["detail"]
        == "Product was changed by another request; reload and retry"
    )
    assert foreign.status_code == 404


def test_concurrent_product_updates_allow_only_one_version_winner(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    sample_product: models.Product,
) -> None:
    headers = auth_headers(tenant_admin)

    def update(name: str):
        return client.patch(
            f"/products/{sample_product.id}",
            headers=headers,
            json={"expected_version": 1, "name": name},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(update, ["Concurrent A", "Concurrent B"]))

    assert sorted(response.status_code for response in responses) == [200, 409]


def test_list_products_supports_pagination_and_validates_bounds(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    sample_product: models.Product,
) -> None:
    headers = auth_headers(tenant_admin)
    first_page = client.get("/products/?skip=0&limit=1", headers=headers)
    empty_page = client.get("/products/?skip=1&limit=1", headers=headers)
    invalid_page = client.get("/products/?skip=-1", headers=headers)
    over_limit = client.get("/products/?limit=101", headers=headers)
    huge_offset = client.get("/products/?skip=1000001", headers=headers)

    assert first_page.status_code == 200
    assert [item["id"] for item in first_page.json()] == [sample_product.id]
    assert empty_page.status_code == 200
    assert empty_page.json() == []
    assert invalid_page.status_code == 422
    assert over_limit.status_code == 422
    assert huge_offset.status_code == 422


def test_product_list_search_and_filters_are_validated_and_scoped(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    sample_product: models.Product,
    other_tenant_product: models.Product,
) -> None:
    headers = auth_headers(tenant_admin)
    search = client.get(
        "/products/",
        headers=headers,
        params={"search": "sample", "min_price": "10", "max_price": "20"},
    )
    invalid_search = client.get("/products/", headers=headers, params={"search": " "})
    invalid_currency = client.get(
        "/products/",
        headers=headers,
        params={"currency": "ZZZ"},
    )
    invalid_bounds = client.get(
        "/products/",
        headers=headers,
        params={"min_price": "30", "max_price": "10"},
    )

    assert search.status_code == 200
    assert [item["id"] for item in search.json()] == [sample_product.id]
    assert other_tenant_product.id not in {item["id"] for item in search.json()}
    assert invalid_search.status_code == 422
    assert invalid_currency.status_code == 422
    assert invalid_bounds.status_code == 422


def test_delete_product_returns_no_content_and_removes_product(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    sample_product: models.Product,
) -> None:
    response = client.delete(
        f"/products/{sample_product.id}",
        headers=auth_headers(tenant_admin),
    )

    assert response.status_code == 204
    assert response.content == b""
    listing = client.get("/products/", headers=auth_headers(tenant_admin))
    assert listing.status_code == 200
    assert listing.json() == []


def test_delete_missing_product_returns_not_found(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
) -> None:
    response = client.delete("/products/999", headers=auth_headers(tenant_admin))

    assert response.status_code == 404


def test_tenant_admin_cannot_delete_another_tenants_product(
    client: TestClient,
    auth_headers: Callable[[models.User], dict[str, str]],
    tenant_admin: models.User,
    other_tenant_product: models.Product,
) -> None:
    response = client.delete(
        f"/products/{other_tenant_product.id}",
        headers=auth_headers(tenant_admin),
    )

    assert response.status_code == 404
