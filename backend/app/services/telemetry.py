from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    FraudCase,
    MakerCheckerRequest,
    ReconciliationException,
    SwitchTransaction,
    SystemAlert,
)


def _percentile(sorted_vals: list[int], pct: float) -> int:
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, max(0, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return int(sorted_vals[idx])


class TelemetryService:
    """Computes live transaction latency, throughput, error rates, and operational health."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def get_ops_telemetry(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        one_min_ago = now - timedelta(minutes=1)
        five_min_ago = now - timedelta(minutes=5)

        async with self.session_factory() as session:
            recent_txs = list(
                (
                    await session.execute(
                        select(SwitchTransaction)
                        .order_by(SwitchTransaction.created_at.desc())
                        .limit(500)
                    )
                )
                .scalars()
                .all()
            )
            open_recon = int(
                (
                    await session.execute(
                        select(func.count(ReconciliationException.id)).where(
                            ReconciliationException.status.in_(["OPEN", "PENDING_ADJUSTMENT"])
                        )
                    )
                ).scalar_one()
            )
            pending_fraud = int(
                (
                    await session.execute(
                        select(func.count(FraudCase.id)).where(FraudCase.status == "PENDING_REVIEW")
                    )
                ).scalar_one()
            )
            pending_approvals = int(
                (
                    await session.execute(
                        select(func.count(MakerCheckerRequest.id)).where(
                            MakerCheckerRequest.status == "PENDING"
                        )
                    )
                ).scalar_one()
            )
            unacked_alerts = int(
                (
                    await session.execute(
                        select(func.count(SystemAlert.id)).where(
                            SystemAlert.acknowledged.is_(False)
                        )
                    )
                ).scalar_one()
            )

        latencies = sorted(
            int(tx.processing_latency_ms)
            for tx in recent_txs
            if tx.processing_latency_ms is not None and tx.processing_latency_ms >= 0
        )
        tx_1m = sum(1 for tx in recent_txs if tx.created_at >= one_min_ago)
        tx_5m = sum(1 for tx in recent_txs if tx.created_at >= five_min_ago)

        by_status: dict[str, int] = {}
        by_corridor: dict[str, dict[str, int]] = {}
        for tx in recent_txs:
            by_status[tx.status] = by_status.get(tx.status, 0) + 1
            corridor = f"{tx.originator_code} -> {tx.beneficiary_code}"
            bucket = by_corridor.setdefault(corridor, {"count": 0, "volume_centavos": 0})
            bucket["count"] += 1
            if tx.status == "COMPLETED":
                bucket["volume_centavos"] += tx.amount_centavos

        total = len(recent_txs)
        completed = by_status.get("COMPLETED", 0)
        success_rate_pct = round((completed / total) * 100.0, 1) if total > 0 else 100.0

        return {
            "sample_size": total,
            "tx_last_1m": tx_1m,
            "tx_last_5m": tx_5m,
            "tps_1m": round(tx_1m / 60.0, 2),
            "success_rate_pct": success_rate_pct,
            "latency_p50_ms": _percentile(latencies, 50),
            "latency_p95_ms": _percentile(latencies, 95),
            "latency_p99_ms": _percentile(latencies, 99),
            "latency_avg_ms": int(sum(latencies) / len(latencies)) if latencies else 0,
            "status_breakdown": by_status,
            "corridor_breakdown": [
                {"corridor": k, "count": v["count"], "volume_centavos": v["volume_centavos"]}
                for k, v in sorted(by_corridor.items())
            ],
            "open_recon_exceptions": open_recon,
            "pending_fraud_reviews": pending_fraud,
            "pending_maker_checker": pending_approvals,
            "unacknowledged_alerts": unacked_alerts,
            "computed_at": now.isoformat(),
        }
