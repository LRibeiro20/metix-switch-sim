from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.security import (
    DEFAULT_ENCRYPTION_KEY,
    DEFAULT_JWT_SECRET,
    DEFAULT_SIGNING_MASTER_SEED,
)


class CbsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CBS_", extra="ignore")

    bank_code: str = "BCI_MZ"
    bank_name: str = "Banco Comercial e de Investimentos"
    bic_code: str = "CGDIMZMA"
    service_id: str = "bci-cbs"
    database_url: str = "postgresql+asyncpg://bci_user:bci_secret@localhost:5434/bci_cbs"
    redis_url: str = "redis://localhost:6380/0"

    currency_code: str = "943"
    timezone_name: str = "Africa/Maputo"
    certs_dir: str = "./certs"
    mtls_enabled: bool = False
    jwt_secret: str = DEFAULT_JWT_SECRET
    encryption_key: str = DEFAULT_ENCRYPTION_KEY
    signing_master_seed: str = DEFAULT_SIGNING_MASTER_SEED
    require_signed_requests: bool = True


@lru_cache
def get_cbs_settings() -> CbsSettings:
    return CbsSettings()
