import hashlib
import hmac
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from pwdlib import PasswordHash
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import settings
from app.database import get_db

password_hash = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = password_hash.hash("not-a-real-account-password")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return password_hash.verify(plain_password, hashed_password)


def create_access_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    payload = data.copy()
    now = datetime.now(UTC)
    expires_at = now + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload["exp"] = expires_at
    payload["iat"] = now
    payload["jti"] = str(uuid4())
    return jwt.encode(
        payload,
        settings.SECRET_KEY.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> models.User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise unauthorized from exc

    email = payload.get("sub")
    if not isinstance(email, str) or not email:
        raise unauthorized

    result = await db.execute(select(models.User).where(models.User.email == email))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active or user.role not in models.USER_ROLES:
        raise unauthorized

    return user


class RequireRole:
    def __init__(self, allowed_roles: list[models.UserRole]) -> None:
        self.allowed_roles = frozenset(allowed_roles)

    async def __call__(
        self,
        user: models.User = Depends(get_current_user),
    ) -> models.User:
        if user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user


class RateLimit:
    def __init__(self, scope: str) -> None:
        self.scope = scope

    async def __call__(
        self,
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> None:
        if self.scope == "login":
            limit = settings.LOGIN_RATE_LIMIT_PER_WINDOW
            window_seconds = settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS
        elif self.scope == "registration":
            limit = settings.REGISTRATION_RATE_LIMIT_PER_WINDOW
            window_seconds = settings.REGISTRATION_RATE_LIMIT_WINDOW_SECONDS
        else:
            raise ValueError(f"Unsupported rate-limit scope: {self.scope}")

        client_host = request.client.host if request.client is not None else "unknown"
        material = f"{self.scope}:{client_host}".encode("utf-8")
        key = hmac.new(
            settings.SECRET_KEY.get_secret_value().encode("utf-8"),
            material,
            hashlib.sha256,
        ).hexdigest()
        now = datetime.now(UTC)
        now_epoch = int(now.timestamp())
        window_start = now_epoch // window_seconds * window_seconds

        await db.execute(
            delete(models.AuthRateLimitBucket).where(
                models.AuthRateLimitBucket.window_start
                < now_epoch
                - max(
                    settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
                    settings.REGISTRATION_RATE_LIMIT_WINDOW_SECONDS,
                ),
            )
        )
        upsert = (
            sqlite_insert(models.AuthRateLimitBucket)
            .values(key=key, window_start=window_start, attempts=1)
            .on_conflict_do_update(
                index_elements=[
                    models.AuthRateLimitBucket.key,
                    models.AuthRateLimitBucket.window_start,
                ],
                set_={
                    "attempts": models.AuthRateLimitBucket.attempts + 1,
                },
            )
        )
        await db.execute(upsert)
        attempts = await db.scalar(
            select(models.AuthRateLimitBucket.attempts).where(
                models.AuthRateLimitBucket.key == key,
                models.AuthRateLimitBucket.window_start == window_start,
            )
        )
        await db.commit()

        if attempts is not None and attempts > limit:
            retry_after = window_start + window_seconds - now_epoch
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests; try again later",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
