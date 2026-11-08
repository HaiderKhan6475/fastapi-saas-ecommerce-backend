from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, schemas
from app.database import get_db
from app.services import tenant_service

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.post(
    "/",
    response_model=schemas.TenantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_tenant(
    registration: schemas.TenantRegistration,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(deps.RateLimit("registration")),
) -> schemas.TenantResponse:
    tenant_data = schemas.TenantCreate(
        name=registration.name,
        slug=registration.slug,
        default_currency=registration.default_currency,
    )
    try:
        tenant = await tenant_service.create_tenant(
            db=db,
            tenant_data=tenant_data,
            admin_email=str(registration.admin_email),
            admin_password=registration.admin_password,
        )
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant registration could not be completed",
        ) from exc

    return schemas.TenantResponse.model_validate(tenant)
