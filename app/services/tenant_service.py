from sqlalchemy.ext.asyncio import AsyncSession

from app import deps, models, schemas


async def create_tenant(
    db: AsyncSession,
    tenant_data: schemas.TenantCreate,
    admin_email: str,
    admin_password: str,
) -> models.Tenant:
    tenant = models.Tenant(**tenant_data.model_dump())
    admin_user = models.User(
        email=admin_email,
        hashed_password=deps.hash_password(admin_password),
        role="TenantAdmin",
        tenant=tenant,
    )
    db.add_all([tenant, admin_user])
    await db.commit()
    return tenant
