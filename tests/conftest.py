import os

# Must be set BEFORE the app (and therefore app.config) is imported.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://stockguard:stockguard@localhost:5432/stockguard_test",
)
os.environ["ADMIN_API_KEY"] = "test-admin-key"
os.environ["SWEEPER_ENABLED"] = "false"  # tests call sweep_once() explicitly

from collections.abc import AsyncIterator  # noqa: E402

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema() -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
