from __future__ import annotations

import time
from dataclasses import dataclass

import redis.asyncio as redis

from app.config import Settings, get_settings
from app.db.models import AccountTier


@dataclass(frozen=True)
class FraudEvaluation:
    risk_score: int
    decision: str  # ALLOW | REVIEW_QUEUE | BLOCK
    triggered_rules: list[dict[str, object]]


class FraudEngine:
    """Real-time pre-route fraud detection, velocity checks, and suspicious pattern scoring."""

    def __init__(self, redis_client: redis.Redis, settings: Settings | None = None) -> None:
        self.redis = redis_client
        self.settings = settings or get_settings()

    async def evaluate_transfer(
        self,
        *,
        originator_code: str,
        originator_account: str,
        originator_tier: str,
        beneficiary_code: str,
        beneficiary_account: str,
        amount_centavos: int,
        daily_used_before_centavos: int,
    ) -> FraudEvaluation:
        now = time.time()
        rules: list[dict[str, object]] = []
        score = 0

        # 1. Sliding-window velocity check (1-minute and 10-minute windows in Redis sorted sets)
        vel_1m_key = f"metix:fraud:vel1m:{originator_account}"
        vel_10m_key = f"metix:fraud:vel10m:{originator_account}"
        member = f"{now:.6f}:{amount_centavos}"

        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(vel_1m_key, 0, now - 60)
        pipe.zadd(vel_1m_key, {member: now})
        pipe.zcard(vel_1m_key)
        pipe.expire(vel_1m_key, 120)

        pipe.zremrangebyscore(vel_10m_key, 0, now - 600)
        pipe.zadd(vel_10m_key, {member: now})
        pipe.zcard(vel_10m_key)
        pipe.expire(vel_10m_key, 720)
        res = await pipe.execute()

        tx_1m = int(res[2])
        tx_10m = int(res[6])

        if tx_1m >= self.settings.fraud_velocity_1m_threshold:
            weight = 55 if tx_1m >= self.settings.fraud_velocity_1m_threshold + 2 else 35
            score += weight
            rules.append(
                {
                    "rule": "VELOCITY_1M_BURST",
                    "weight": weight,
                    "detail": (
                        f"{tx_1m} transfers in 60s "
                        f"(threshold >= {self.settings.fraud_velocity_1m_threshold})"
                    ),
                }
            )

        if tx_10m >= self.settings.fraud_velocity_10m_threshold:
            score += 25
            rules.append(
                {
                    "rule": "VELOCITY_10M_SUSTAINED",
                    "weight": 25,
                    "detail": (
                        f"{tx_10m} transfers in 10m "
                        f"(threshold >= {self.settings.fraud_velocity_10m_threshold})"
                    ),
                }
            )

        # 2. Fan-out check (distinct beneficiaries in 10m)
        fanout_key = f"metix:fraud:fanout:{originator_account}"
        await self.redis.sadd(fanout_key, f"{beneficiary_code}:{beneficiary_account}")
        await self.redis.expire(fanout_key, 600)
        distinct_bens = int(await self.redis.scard(fanout_key))
        if distinct_bens >= 4:
            score += 20
            rules.append(
                {
                    "rule": "FAN_OUT_BENEFICIARIES",
                    "weight": 20,
                    "detail": f"Sent to {distinct_bens} distinct beneficiaries within 10 minutes",
                }
            )

        # 3. Rapid Round-Trip / Layering Detection (B -> A shortly after A -> B for similar amount)
        reverse_pair_key = f"metix:fraud:pair:{beneficiary_account}:{originator_account}"
        forward_pair_key = f"metix:fraud:pair:{originator_account}:{beneficiary_account}"
        prev_rev_raw = await self.redis.get(reverse_pair_key)
        await self.redis.set(forward_pair_key, str(amount_centavos), ex=300)

        if prev_rev_raw is not None:
            prev_amt = int(
                prev_rev_raw.decode() if isinstance(prev_rev_raw, bytes) else prev_rev_raw
            )
            # Only trigger round-trip risk on larger transfers (>= 5,000.00 MT) so small P2P
            # repayments / symmetric smoke tests do not false-positive.
            if (
                prev_amt > 0
                and abs(prev_amt - amount_centavos) <= max(10_000, int(prev_amt * 0.15))
                and amount_centavos >= 500_000
            ):
                score += 55
                rules.append(
                    {
                        "rule": "RAPID_ROUND_TRIP_LAYERING",
                        "weight": 55,
                        "detail": (
                            f"Reverse corridor {beneficiary_account} -> {originator_account} "
                            f"seen within 5m for {prev_amt / 100:,.2f} MT"
                        ),
                    }
                )

        # 4. Structuring / Smurfing just below Notice No. 1/GBM/2026 cap
        cap = (
            self.settings.business_daily_limit_centavos
            if originator_tier == AccountTier.BUSINESS.value
            else self.settings.individual_daily_limit_centavos
        )
        # Multiple transfers in 10m whose cumulative value approaches >= 90% of daily cap
        projected_daily = daily_used_before_centavos + amount_centavos
        if (
            tx_10m >= 3
            and daily_used_before_centavos > 0
            and int(cap * 0.90) <= projected_daily <= cap
        ):
            score += 35
            rules.append(
                {
                    "rule": "STRUCTURING_NEAR_REGULATORY_CAP",
                    "weight": 35,
                    "detail": (
                        f"{tx_10m} split transfers reaching {projected_daily / 100:,.2f} MT "
                        f"(>=90% of {cap / 100:,.2f} MT Notice 1/GBM/2026 daily cap)"
                    ),
                }
            )

        # 5. Single-Transfer High-Value Anomaly (>= 95% of tier cap in one shot with prior velocity)
        if amount_centavos >= int(cap * 0.95) and amount_centavos <= cap and tx_1m >= 2:
            score += 30
            rules.append(
                {
                    "rule": "HIGH_VALUE_VELOCITY_SPIKE",
                    "weight": 30,
                    "detail": (
                        f"High-value transfer ({amount_centavos / 100:,.2f} MT) combined with "
                        f"rapid repeat velocity ({tx_1m}/min)"
                    ),
                }
            )

        score = min(100, score)
        if score >= self.settings.fraud_block_threshold:
            decision = "BLOCK"
        elif score >= self.settings.fraud_review_threshold:
            decision = "REVIEW_QUEUE"
        else:
            decision = "ALLOW"

        return FraudEvaluation(risk_score=score, decision=decision, triggered_rules=rules)
