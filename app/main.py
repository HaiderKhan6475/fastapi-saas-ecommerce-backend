import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.observability import install_operational_handling
from app.routers import auth, carts, health, orders, payments, products, tenants
from app.services import order_service


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    stop_expiration_worker = asyncio.Event()
    expiration_worker = asyncio.create_task(
        order_service.run_expiration_worker(
            AsyncSessionLocal,
            stop_expiration_worker,
            poll_interval_seconds=settings.ORDER_EXPIRATION_POLL_INTERVAL_SECONDS,
            batch_size=settings.ORDER_EXPIRATION_BATCH_SIZE,
        )
    )
    try:
        yield
    finally:
        stop_expiration_worker.set()
        await expiration_worker
        await engine.dispose()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

install_operational_handling(
    app,
    request_timeout_seconds=settings.REQUEST_TIMEOUT_SECONDS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

app.include_router(auth.router)
app.include_router(tenants.router)
app.include_router(products.router)
app.include_router(carts.router)
app.include_router(orders.router)
app.include_router(payments.router)
app.include_router(health.router)
