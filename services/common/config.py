"""Settings shared by every demo service, loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "production"]


class ServiceSettings(BaseSettings):
    """One settings object per service process.

    ``SERVICE_NAME`` is what distinguishes the three services; everything else
    is shared. See ``.env.example`` for the full list.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    service_name: str = "unknown-service"
    environment: Environment = "local"
    log_level: str = "INFO"

    # --- Database ---------------------------------------------------------
    # The "bad config" chaos scenario works by overriding this with a wrong
    # password, so the value must come from the environment, never a literal.
    database_url: str = "postgresql+asyncpg://aiops:aiops@localhost:5432/aiops"
    db_echo: bool = False
    #: Deliberately small so the "database exhaustion" scenario is reachable.
    db_pool_size: int = Field(default=3, ge=1)
    db_max_overflow: int = Field(default=2, ge=0)
    db_pool_timeout: float = Field(default=3.0, gt=0)
    #: Connections older than this are discarded and reopened on next checkout.
    #: Short on purpose. `pool_pre_ping` keeps a *live* connection forever, so
    #: without recycling nothing in the system ever asks Postgres for a new
    #: connection - and the database-exhaustion scenario stays completely
    #: invisible: the server refuses every new client while all three services
    #: happily reuse the connections they already hold. Recycling is standard
    #: practice regardless; the short interval is what makes the fault
    #: observable within one interval instead of never.
    db_pool_recycle_seconds: int = Field(default=30, ge=1)

    # --- Downstream services (orders calls these) -------------------------
    #: orders_url is only used by the console (orders does not call itself),
    #: but it lives here so every URL is configured in one place.
    orders_url: str = "http://localhost:8001"
    payments_url: str = "http://localhost:8002"
    inventory_url: str = "http://localhost:8003"
    downstream_timeout_seconds: float = Field(default=10.0, gt=0)

    # --- Observability ----------------------------------------------------
    #: How many recent log records each service keeps in memory for `GET /_logs`.
    log_buffer_size: int = Field(default=500, ge=10, le=10_000)

    # --- Chaos ------------------------------------------------------------
    #: Gates the `/_chaos/*` control endpoints. Local demo only - a real
    #: service would never ship a fault-injection API.
    chaos_enabled: bool = True

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return level


@lru_cache
def get_settings() -> ServiceSettings:
    """Cached settings accessor so the environment is parsed once per process."""
    return ServiceSettings()
