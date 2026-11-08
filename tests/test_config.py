from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings

VALID_SETTINGS: dict[str, Any] = {
    "APP_NAME": "Test API",
    "ENVIRONMENT": "production",
    "DATABASE_URL": "sqlite+aiosqlite:///./test.db",
    "SECRET_KEY": "aB3dE5gH7jK9mN2pQ4sT6vW8xY1zA3cD",
    "JWT_ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": 30,
    "CORS_ORIGINS": ["https://shop.example.com"],
}


def build_settings(**overrides: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        **(VALID_SETTINGS | overrides),
    )


@pytest.mark.parametrize(
    "missing_setting",
    [
        "APP_NAME",
        "ENVIRONMENT",
        "DATABASE_URL",
        "SECRET_KEY",
        "JWT_ALGORITHM",
        "ACCESS_TOKEN_EXPIRE_MINUTES",
        "CORS_ORIGINS",
    ],
)
def test_required_settings_cannot_be_missing(
    missing_setting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for setting in VALID_SETTINGS:
        monkeypatch.delenv(setting, raising=False)

    values = VALID_SETTINGS.copy()
    values.pop(missing_setting)

    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            **values,
        )


def test_development_accepts_local_placeholder_and_masks_secret() -> None:
    settings = build_settings(
        ENVIRONMENT="development",
        SECRET_KEY="development-only-placeholder",
        CORS_ORIGINS=["http://localhost:3000"],
    )

    assert isinstance(settings.SECRET_KEY, SecretStr)
    assert settings.SECRET_KEY.get_secret_value() == "development-only-placeholder"
    assert "development-only-placeholder" not in repr(settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"ENVIRONMENT": "staging"},
        {"DATABASE_URL": "sqlite:///./sync.db"},
        {"DATABASE_URL": "not-a-database-url"},
        {"JWT_ALGORITHM": "none"},
        {"ACCESS_TOKEN_EXPIRE_MINUTES": 0},
        {"REQUEST_TIMEOUT_SECONDS": 0},
        {"REQUEST_TIMEOUT_SECONDS": 301},
        {"ORDER_EXPIRATION_POLL_INTERVAL_SECONDS": 0},
        {"ORDER_EXPIRATION_POLL_INTERVAL_SECONDS": 3601},
        {"ORDER_EXPIRATION_BATCH_SIZE": 0},
        {"ORDER_EXPIRATION_BATCH_SIZE": 1001},
        {"LOGIN_RATE_LIMIT_PER_WINDOW": 0},
        {"LOGIN_RATE_LIMIT_WINDOW_SECONDS": 0},
        {"REGISTRATION_RATE_LIMIT_PER_WINDOW": 0},
        {"REGISTRATION_RATE_LIMIT_WINDOW_SECONDS": 0},
        {"CORS_ORIGINS": ["*"]},
        {"CORS_ORIGINS": ["https://shop.example.com/path"]},
        {"ENVIRONMENT": "production", "CORS_ORIGINS": ["http://shop.example.com"]},
        {"ENVIRONMENT": "production", "CORS_ORIGINS": []},
        {"ENVIRONMENT": "production", "SECRET_KEY": "short"},
        {
            "ENVIRONMENT": "production",
            "JWT_ALGORITHM": "HS512",
            "SECRET_KEY": "aB3dE5gH7jK9mN2pQ4sT6vW8xY1zA3cD",
        },
        {
            "ENVIRONMENT": "production",
            "SECRET_KEY": "change-me-" + "A1b2C3d4E5f6G7h8J9k0",
        },
    ],
)
def test_invalid_settings_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        build_settings(**overrides)


def test_valid_production_settings_are_accepted() -> None:
    settings = build_settings()

    assert settings.ENVIRONMENT == "production"
    assert settings.DATABASE_URL.startswith("sqlite+aiosqlite:")
    assert settings.ORDER_PENDING_TTL_MINUTES == 30


@pytest.mark.parametrize("ttl_minutes", [0, -1, 10081])
def test_invalid_order_pending_ttl_is_rejected(ttl_minutes: int) -> None:
    with pytest.raises(ValidationError):
        build_settings(ORDER_PENDING_TTL_MINUTES=ttl_minutes)


def test_order_pending_ttl_accepts_configured_bounds() -> None:
    assert build_settings(ORDER_PENDING_TTL_MINUTES=1).ORDER_PENDING_TTL_MINUTES == 1
    assert (
        build_settings(ORDER_PENDING_TTL_MINUTES=10080).ORDER_PENDING_TTL_MINUTES
        == 10080
    )


def test_invalid_secret_is_not_exposed_in_validation_error() -> None:
    secret = "private-looking-test-secret"

    with pytest.raises(ValidationError) as error:
        build_settings(SECRET_KEY=secret)

    assert secret not in str(error.value)
