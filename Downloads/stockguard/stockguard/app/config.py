import re
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://stockguard:stockguard@localhost:5432/stockguard"
    admin_api_key: str = "change-me"

    # How a reservation claims stock. "naive" is a deliberately racy baseline
    # used only to demonstrate overselling in the load test.
    inventory_strategy: Literal["atomic", "pessimistic", "optimistic", "naive"] = "atomic"
    optimistic_max_retries: int = 8

    reservation_ttl_seconds: int = 600
    sweeper_enabled: bool = True
    sweep_interval_seconds: float = 5.0
    idempotency_ttl_hours: int = 24

    db_pool_size: int = 20
    db_max_overflow: int = 20
    create_tables_on_startup: bool = True
    log_level: str = "INFO"

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_database_url(cls, value: object) -> object:
        """Accept the URL shapes hosting providers hand out and make them asyncpg-safe."""
        if not isinstance(value, str):
            return value
        if value.startswith("postgres://"):
            value = "postgresql+asyncpg://" + value[len("postgres://"):]
        elif value.startswith("postgresql://"):
            value = "postgresql+asyncpg://" + value[len("postgresql://"):]
        # asyncpg calls it `ssl`, libpq calls it `sslmode`.
        return re.sub(r"([?&])sslmode=", r"\1ssl=", value)


settings = Settings()
