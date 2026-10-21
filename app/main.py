import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from sqlalchemy import text

from app.api import health, orders, products, reservations
from app.config import settings
from app.db import engine
from app.errors import register_error_handlers
from app.models import Base
from app.services.sweeper import run_sweeper

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("stockguard")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if settings.admin_api_key == "change-me":
        logger.warning("ADMIN_API_KEY is the default value; set a real secret before deploying")
    if settings.inventory_strategy == "naive":
        logger.warning("INVENTORY_STRATEGY=naive is intentionally racy: benchmarking only!")

    if settings.create_tables_on_startup:
        async with engine.begin() as conn:
            # Serialise schema creation when several replicas boot together.
            await conn.execute(text("SELECT pg_advisory_xact_lock(727274)"))
            await conn.run_sync(Base.metadata.create_all)

    sweeper = (
        asyncio.create_task(run_sweeper(), name="reservation-sweeper")
        if settings.sweeper_enabled
        else None
    )
    try:
        yield
    finally:
        if sweeper is not None:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
        await engine.dispose()


app = FastAPI(
    title="StockGuard",
    description="Inventory reservation and order service with oversell protection.",
    version="1.0.0",
    lifespan=lifespan,
)
register_error_handlers(app)
app.include_router(health.router)
app.include_router(products.router)
app.include_router(reservations.router)
app.include_router(orders.router)
