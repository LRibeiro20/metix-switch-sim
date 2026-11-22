from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "metix-switch-sim"
    database_url: str = "postgresql+asyncpg://metix:metix_secret@localhost:5433/metix_switch"
    redis_url: str = "redis://localhost:6380/0"

    # Regulatory & SPIM SLA parameters (Banco de Moçambique Notice No. 1/GBM/2026)
    currency_code: str = "943"  # ISO 4217 numeric code for MZN
    spim_sla_seconds: int = 25
    individual_daily_limit_centavos: int = 20_000_000  # 200,000.00 MT
    business_daily_limit_centavos: int = 50_000_000  # 500,000.00 MT
    idempotency_ttl_seconds: int = 86_400  # 24 hours
    timezone_name: str = "Africa/Maputo"  # CAT (UTC+2)

    # Beneficiary simulator HTTP target (self in local dev or switch-api in Docker)
    beneficiary_base_url: str = "http://localhost:8000"
    sweeper_poll_interval_seconds: float = 1.0
    run_inline_sweeper: bool = False

    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
