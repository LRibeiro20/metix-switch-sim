from __future__ import annotations

import json

import redis.asyncio as redis
from pydantic import BaseModel, Field

CHAOS_REDIS_KEY = "metix:chaos:config"


class ChaosConfig(BaseModel):
    """Runtime chaos controls for the Beneficiary PSP / Mobile Wallet Simulator."""

    preset: str = Field(default="normal")
    latency_ms: int = Field(default=200, ge=0, le=60_000)
    drop_0210: bool = Field(default=False)
    force_de39: str | None = Field(default=None, pattern=r"^[A-Z0-9]{2}$")
    fail_0430: bool = Field(default=False)
    target_beneficiary: str | None = Field(
        default=None,
        description="Optional institution code (e.g. MPESA_MZ) to scope chaos; null = all.",
    )


async def get_chaos_config(redis_client: redis.Redis) -> ChaosConfig:
    raw = await redis_client.get(CHAOS_REDIS_KEY)
    if raw is None:
        return ChaosConfig()
    data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    return ChaosConfig.model_validate(data)


async def set_chaos_config(redis_client: redis.Redis, config: ChaosConfig) -> ChaosConfig:
    await redis_client.set(CHAOS_REDIS_KEY, config.model_dump_json())
    return config
