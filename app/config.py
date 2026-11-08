from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str
    ENVIRONMENT: Literal["development", "test", "production"]
    DATABASE_URL: str
    SECRET_KEY: SecretStr
    JWT_ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int
    CORS_ORIGINS: list[str]
    REQUEST_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0, le=300)
    ORDER_PENDING_TTL_MINUTES: int = Field(default=30, gt=0, le=10080)
    ORDER_EXPIRATION_POLL_INTERVAL_SECONDS: int = Field(default=60, gt=0, le=3600)
    ORDER_EXPIRATION_BATCH_SIZE: int = Field(default=100, gt=0, le=1000)
    LOGIN_RATE_LIMIT_PER_WINDOW: int = Field(default=10, gt=0)
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=900, gt=0)
    REGISTRATION_RATE_LIMIT_PER_WINDOW: int = Field(default=5, gt=0)
    REGISTRATION_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=3600, gt=0)

    @field_validator("ENVIRONMENT", mode="before")
    @classmethod
    def normalize_environment(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        try:
            url = make_url(value)
        except (ArgumentError, TypeError, ValueError) as exc:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from exc

        if url.drivername != "sqlite+aiosqlite":
            raise ValueError("DATABASE_URL must use the async SQLite driver")
        if url.username is not None or url.password is not None:
            raise ValueError("SQLite DATABASE_URL must not contain credentials")
        return value

    @field_validator("CORS_ORIGINS")
    @classmethod
    def validate_cors_origins(cls, value: list[str]) -> list[str]:
        origins: list[str] = []
        for origin in value:
            origin = origin.strip()
            try:
                parsed = urlsplit(origin)
                has_host = parsed.hostname is not None
            except ValueError as exc:
                raise ValueError(
                    "CORS_ORIGINS must contain valid HTTP(S) origins"
                ) from exc

            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or not has_host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("CORS_ORIGINS must contain valid HTTP(S) origins")
            origins.append(origin.removesuffix("/") if parsed.path == "/" else origin)
        return origins

    @model_validator(mode="after")
    def validate_security_settings(self) -> "Settings":
        if not self.APP_NAME.strip():
            raise ValueError("APP_NAME must not be empty")
        if self.JWT_ALGORITHM not in {"HS256", "HS384", "HS512"}:
            raise ValueError("JWT_ALGORITHM must be an approved HMAC algorithm")
        if self.ACCESS_TOKEN_EXPIRE_MINUTES <= 0:
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES must be positive")

        secret = self.SECRET_KEY.get_secret_value()
        if not secret.strip():
            raise ValueError("SECRET_KEY must not be empty")

        if self.ENVIRONMENT == "production":
            insecure_markers = (
                "change-me",
                "changeme",
                "replace-me",
                "replace-with",
                "your-secret",
                "placeholder",
                "example",
                "default-secret",
                "development-secret",
                "test-secret",
            )
            minimum_key_bytes = {
                "HS256": 32,
                "HS384": 48,
                "HS512": 64,
            }[self.JWT_ALGORITHM]
            if (
                len(secret.encode("utf-8")) < minimum_key_bytes
                or len(set(secret)) < 12
                or any(marker in secret.lower() for marker in insecure_markers)
            ):
                raise ValueError(
                    "Production SECRET_KEY must be independently generated and "
                    "long enough for the configured JWT algorithm"
                )
            if not self.CORS_ORIGINS:
                raise ValueError("Production requires at least one CORS origin")
            if any(
                not origin.lower().startswith("https://")
                for origin in self.CORS_ORIGINS
            ):
                raise ValueError("Production CORS origins must use HTTPS")

        return self


settings = Settings()  # type: ignore[call-arg]
