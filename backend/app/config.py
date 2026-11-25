from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.security import (
    DEFAULT_ENCRYPTION_KEY,
    DEFAULT_JWT_SECRET,
    DEFAULT_SIGNING_MASTER_SEED,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "metix-switch-sim"
    service_id: str = "switch-api"
    database_url: str = "postgresql+asyncpg://metix:metix_secret@localhost:5433/metix_switch"
    redis_url: str = "redis://localhost:6380/0"

    # Independent Core Banking Simulator & Wallet Adapter endpoints
    bci_cbs_base_url: str = "http://localhost:8001"
    stdb_cbs_base_url: str = "http://localhost:8002"
    wallet_sim_base_url: str = "http://localhost:8003"

    # Regulatory & SPIM SLA parameters (Banco de Moçambique Notice No. 1/GBM/2026)
    currency_code: str = "943"  # ISO 4217 numeric code for MZN
    spim_sla_seconds: int = 25
    individual_daily_limit_centavos: int = 20_000_000  # 200,000.00 MT
    business_daily_limit_centavos: int = 50_000_000  # 500,000.00 MT
    idempotency_ttl_seconds: int = 86_400  # 24 hours
    timezone_name: str = "Africa/Maputo"  # CAT (UTC+2)

    # Fraud & velocity detection thresholds
    fraud_velocity_1m_threshold: int = 6
    fraud_velocity_10m_threshold: int = 12
    fraud_review_threshold: int = 50
    fraud_block_threshold: int = 80

    # Enterprise Security (mTLS, OAuth 2.0, Ed25519, AES-256-GCM)
    certs_dir: str = "./certs"
    mtls_enabled: bool = False
    jwt_secret: str = DEFAULT_JWT_SECRET
    encryption_key: str = DEFAULT_ENCRYPTION_KEY
    signing_master_seed: str = DEFAULT_SIGNING_MASTER_SEED
    auth_enforce_transfers: bool = True

    sweeper_poll_interval_seconds: float = 1.0
    run_inline_sweeper: bool = False

    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
