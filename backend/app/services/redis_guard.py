from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import redis.asyncio as redis

from app.config import Settings, get_settings
from app.db.models import AccountTier

# Atomic Lua script: checks Notice No. 1/GBM/2026 daily limit and increments only if within cap.
_RESERVE_DAILY_LIMIT_LUA = """
local key = KEYS[1]
local amount = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local current = tonumber(redis.call('GET', key) or '0')
if current + amount > limit then
    return {0, current}
end
local updated = redis.call('INCRBY', key, amount)
if redis.call('TTL', key) < 0 then
    redis.call('EXPIRE', key, ttl)
end
return {1, updated}
"""

# Atomic Lua script: releases reserved daily quota on decline or reversal, clamped at 0.
_RELEASE_DAILY_LIMIT_LUA = """
local key = KEYS[1]
local amount = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', key) or '0')
local updated = current - amount
if updated < 0 then
    updated = 0
end
redis.call('SET', key, updated, 'KEEPTTL')
return updated
"""


class RedisGuard:
    """Atomic Redis idempotency locks, STAN generator, and Notice No. 1/GBM/2026 daily limits."""

    def __init__(self, client: redis.Redis, settings: Settings | None = None) -> None:
        self.client = client
        self.settings = settings or get_settings()
        self._reserve_script = self.client.register_script(_RESERVE_DAILY_LIMIT_LUA)
        self._release_script = self.client.register_script(_RELEASE_DAILY_LIMIT_LUA)

    def _today_cat(self) -> str:
        tz = ZoneInfo(self.settings.timezone_name)
        return datetime.now(tz).strftime("%Y-%m-%d")

    def daily_limit_for_tier(self, tier: str) -> int | None:
        if tier == AccountTier.INDIVIDUAL.value:
            return self.settings.individual_daily_limit_centavos
        if tier == AccountTier.BUSINESS.value:
            return self.settings.business_daily_limit_centavos
        return None

    def _daily_limit_key(self, account_id: uuid.UUID, day: str | None = None) -> str:
        return f"metix:daily_limit:{account_id}:{day or self._today_cat()}"

    def _idemp_lock_key(self, idempotency_key: str) -> str:
        return f"metix:idemp:lock:{idempotency_key}"

    def _idemp_tx_key(self, idempotency_key: str) -> str:
        return f"metix:idemp:tx:{idempotency_key}"

    def _stan_rrn_key(self, stan: str, rrn: str) -> str:
        return f"metix:idemp:stanrrn:{stan}:{rrn}"

    async def acquire_idempotency_lock(self, idempotency_key: str) -> bool:
        """Atomically acquire idempotency lock via SET NX EX. Returns True for first caller."""
        token = uuid.uuid4().hex
        acquired = await self.client.set(
            self._idemp_lock_key(idempotency_key),
            token,
            nx=True,
            ex=self.settings.idempotency_ttl_seconds,
        )
        return bool(acquired)

    async def acquire_stan_rrn_lock(self, stan: str, rrn: str, idempotency_key: str) -> bool:
        acquired = await self.client.set(
            self._stan_rrn_key(stan, rrn),
            idempotency_key,
            nx=True,
            ex=self.settings.idempotency_ttl_seconds,
        )
        return bool(acquired)

    async def bind_transaction_id(self, idempotency_key: str, transaction_id: uuid.UUID) -> None:
        await self.client.set(
            self._idemp_tx_key(idempotency_key),
            str(transaction_id),
            ex=self.settings.idempotency_ttl_seconds,
        )

    async def release_idempotency_lock(self, idempotency_key: str) -> None:
        """Release lock only if transaction creation aborted before DB persistence."""
        await self.client.delete(self._idemp_lock_key(idempotency_key))

    async def get_bound_transaction_id(
        self, idempotency_key: str, *, wait_seconds: float = 3.0
    ) -> uuid.UUID | None:
        """Return transaction UUID bound to idempotency_key, waiting briefly for commit."""
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            raw = await self.client.get(self._idemp_tx_key(idempotency_key))
            if raw is not None:
                val = raw.decode() if isinstance(raw, bytes) else str(raw)
                return uuid.UUID(val)
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.05)

    async def next_stan(self) -> str:
        """Generate a monotonically increasing 6-digit zero-padded ISO 8583 STAN."""
        seq = await self.client.incr("metix:seq:stan")
        return f"{int(seq) % 1_000_000:06d}"

    async def reserve_daily_limit(
        self, account_id: uuid.UUID, tier: str, amount_centavos: int
    ) -> tuple[bool, int, int]:
        """Atomically check & reserve Notice No. 1/GBM/2026 daily limit.

        Returns `(allowed, resulting_usage_centavos, cap_centavos)`.
        """
        cap = self.daily_limit_for_tier(tier)
        if cap is None:
            return True, 0, 0
        key = self._daily_limit_key(account_id)
        # 48h TTL guarantees key outlives the CAT calendar day cleanly across timezone boundaries.
        res = await self._reserve_script(keys=[key], args=[amount_centavos, cap, 172_800])
        allowed = int(res[0]) == 1
        usage = int(res[1])
        return allowed, usage, cap

    async def release_daily_limit(self, account_id: uuid.UUID, amount_centavos: int) -> int:
        key = self._daily_limit_key(account_id)
        updated = await self._release_script(keys=[key], args=[amount_centavos])
        return int(updated)

    async def get_daily_usage(self, account_id: uuid.UUID) -> int:
        raw = await self.client.get(self._daily_limit_key(account_id))
        if raw is None:
            return 0
        return int(raw.decode() if isinstance(raw, bytes) else raw)
