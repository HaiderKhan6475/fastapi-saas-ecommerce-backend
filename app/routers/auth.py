from fastapi import APIRouter, Depends, HTTPException, Path, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models, schemas
from app.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=schemas.Token)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(deps.RateLimit("login")),
) -> schemas.Token:
    result = await db.execute(
        select(models.User).where(models.User.email == form_data.username)
    )
    user = result.scalar_one_or_none()

    password_within_limit = len(form_data.password) <= 1024
    password_candidate = form_data.password if password_within_limit else ""
    hashed_password = (
        user.hashed_password if user is not None else deps.DUMMY_PASSWORD_HASH
    )
    password_valid = deps.verify_password(password_candidate, hashed_password)
    if (
        not password_within_limit
        or user is None
        or not user.is_active
        or not password_valid
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = deps.create_access_token(
        {
            "sub": user.email,
            "role": user.role,
            "tenant_id": user.tenant_id,
        }
    )
    return schemas.Token(access_token=access_token, token_type="bearer")


@router.post(
    "/users/{user_id}/deactivate",
    response_model=schemas.UserResponse,
)
async def deactivate_user(
    user_id: int = Path(gt=0),
    _: models.User = Depends(deps.RequireRole(["SuperAdmin"])),
    db: AsyncSession = Depends(get_db),
) -> schemas.UserResponse:
    result = await db.execute(select(models.User).where(models.User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    user.is_active = False
    await db.commit()
    return schemas.UserResponse.model_validate(user)
