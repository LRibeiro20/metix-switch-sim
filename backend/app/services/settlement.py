from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.db.models import (
    Account,
    JournalEntryType,
    ParticipantLiquidityProfile,
    SettlementCycle,
    SettlementPosition,
    SwitchTransaction,
    TransactionStatus,
)
from app.db.seed import (
    BM_RTGS_TREASURY_ACCOUNT_NUMBER,
    SETTLEMENT_ACCOUNT_BY_INSTITUTION,
)
from app.services.audit import create_system_alert, record_audit_log
from app.services.ledger import post_balanced_journal
from shared.security import Principal, sign_document, verify_document_signature


class SettlementService:
    """Multilateral Net Settlement (MNS), liquidity management, and failure simulation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    async def get_live_positions(self) -> dict[str, Any]:
        """Compute live multilateral net positions for all unsettled COMPLETED transfers."""
        async with self.session_factory() as session:
            profiles = list(
                (
                    await session.execute(
                        select(ParticipantLiquidityProfile).order_by(
                            ParticipantLiquidityProfile.institution_code.asc()
                        )
                    )
                )
                .scalars()
                .all()
            )
            accts = {
                a.id: a
                for a in (
                    await session.execute(
                        select(Account).where(
                            Account.id.in_([p.settlement_account_id for p in profiles])
                        )
                    )
                )
                .scalars()
                .all()
            }
            unsettled_txs = list(
                (
                    await session.execute(
                        select(SwitchTransaction).where(
                            SwitchTransaction.status == TransactionStatus.COMPLETED.value,
                            SwitchTransaction.settlement_cycle_id.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )

            debits: dict[str, int] = {p.institution_code: 0 for p in profiles}
            credits: dict[str, int] = {p.institution_code: 0 for p in profiles}
            counts: dict[str, int] = {p.institution_code: 0 for p in profiles}

            for tx in unsettled_txs:
                if tx.originator_code in debits:
                    debits[tx.originator_code] += tx.amount_centavos
                    counts[tx.originator_code] += 1
                if tx.beneficiary_code in credits:
                    credits[tx.beneficiary_code] += tx.amount_centavos
                    counts[tx.beneficiary_code] += 1

            positions: list[dict[str, Any]] = []
            net_sum = 0
            gross_volume = sum(tx.amount_centavos for tx in unsettled_txs)

            for prof in profiles:
                code = prof.institution_code
                acct = accts[prof.settlement_account_id]
                gross_dr = debits[code]
                gross_cr = credits[code]
                net_pos = gross_cr - gross_dr
                net_sum += net_pos

                usable_liquidity = max(0, acct.balance_centavos - prof.minimum_reserve_centavos)
                utilization_bps = 0
                if prof.net_debit_cap_centavos > 0 and net_pos < 0:
                    utilization_bps = min(
                        10_000, int((abs(net_pos) * 10_000) // prof.net_debit_cap_centavos)
                    )

                shortfall = (
                    prof.simulate_shortfall
                    or acct.balance_centavos < prof.minimum_reserve_centavos
                    or (net_pos < 0 and abs(net_pos) > prof.net_debit_cap_centavos)
                )

                pos_status = (
                    "LIQUIDITY_SHORTFALL"
                    if shortfall
                    else "NET_CREDITOR"
                    if net_pos > 0
                    else "NET_DEBTOR"
                    if net_pos < 0
                    else "FLAT"
                )
                positions.append(
                    {
                        "institution_code": code,
                        "settlement_account_number": acct.account_number,
                        "settlement_balance_centavos": acct.balance_centavos,
                        "prefunded_limit_centavos": prof.prefunded_limit_centavos,
                        "minimum_reserve_centavos": prof.minimum_reserve_centavos,
                        "collateral_centavos": prof.collateral_centavos,
                        "net_debit_cap_centavos": prof.net_debit_cap_centavos,
                        "available_liquidity_centavos": usable_liquidity,
                        "gross_debits_centavos": gross_dr,
                        "gross_credits_centavos": gross_cr,
                        "net_position_centavos": net_pos,
                        "tx_count": counts[code],
                        "liquidity_utilization_bps": utilization_bps,
                        "simulate_shortfall": prof.simulate_shortfall,
                        "status": pos_status,
                    }
                )

            return {
                "unsettled_tx_count": len(unsettled_txs),
                "total_gross_volume_centavos": gross_volume,
                "total_net_obligation_centavos": sum(
                    p["net_position_centavos"] for p in positions if p["net_position_centavos"] > 0
                ),
                "netting_zero_sum_valid": net_sum == 0,
                "positions": positions,
            }

    async def set_shortfall_simulation(
        self, institution_code: str, *, enabled: bool, actor: Principal
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            prof = (
                await session.execute(
                    select(ParticipantLiquidityProfile).where(
                        ParticipantLiquidityProfile.institution_code == institution_code
                    )
                )
            ).scalar_one_or_none()
            if prof is None:
                raise KeyError(f"Unknown participant institution: {institution_code}")
            prof.simulate_shortfall = enabled
            await record_audit_log(
                session,
                actor_id=actor.subject,
                actor_role=actor.role.value,
                action="SETTLEMENT_SHORTFALL_SIMULATION_UPDATED",
                resource_type="liquidity_profile",
                resource_id=institution_code,
                payload_json={"simulate_shortfall": enabled},
            )
            await session.commit()
        return {"institution_code": institution_code, "simulate_shortfall": enabled}

    async def execute_mns_cycle(
        self,
        *,
        actor: Principal,
        simulate_failure_institution: str | None = None,
    ) -> SettlementCycle:
        """Execute a Multilateral Net Settlement cycle with liquidity verification & signing."""
        preview = await self.get_live_positions()

        async with self.session_factory() as session:
            max_num = (
                await session.execute(
                    select(func.coalesce(func.max(SettlementCycle.cycle_number), 0))
                )
            ).scalar_one()
            next_cycle_no = int(max_num) + 1

            unsettled_txs = list(
                (
                    await session.execute(
                        select(SwitchTransaction)
                        .where(
                            SwitchTransaction.status == TransactionStatus.COMPLETED.value,
                            SwitchTransaction.settlement_cycle_id.is_(None),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )

            # Check liquidity adequacy across participants
            failed_participants: list[str] = []
            for p in preview["positions"]:
                code = str(p["institution_code"])
                forced_fail = simulate_failure_institution == code
                if forced_fail or p["status"] == "LIQUIDITY_SHORTFALL":
                    failed_participants.append(code)

            now = datetime.now(UTC)
            cycle_status = "FAILED" if failed_participants else "SETTLED"
            failure_reason = (
                f"Liquidity shortfall / reserve breach for participant(s): "
                f"{', '.join(failed_participants)}"
                if failed_participants
                else None
            )

            cycle = SettlementCycle(
                cycle_number=next_cycle_no,
                status=cycle_status,
                total_gross_volume_centavos=int(preview["total_gross_volume_centavos"]),
                total_net_settled_centavos=int(preview["total_net_obligation_centavos"])
                if cycle_status == "SETTLED"
                else 0,
                tx_count=len(unsettled_txs),
                failure_reason=failure_reason,
                initiated_by=actor.subject,
                settled_at=now if cycle_status == "SETTLED" else None,
            )
            session.add(cycle)
            await session.flush()

            report_positions: list[dict[str, Any]] = []
            for p in preview["positions"]:
                code = str(p["institution_code"])
                is_failed = code in failed_participants
                pos_status = (
                    "LIQUIDITY_SHORTFALL"
                    if is_failed
                    else "SETTLED"
                    if cycle_status == "SETTLED"
                    else str(p["status"])
                )
                bal = int(p["settlement_balance_centavos"])
                net_pos = int(p["net_position_centavos"])
                pos = SettlementPosition(
                    cycle_id=cycle.id,
                    institution_code=code,
                    gross_debits_centavos=int(p["gross_debits_centavos"]),
                    gross_credits_centavos=int(p["gross_credits_centavos"]),
                    net_position_centavos=net_pos,
                    tx_count=int(p["tx_count"]),
                    opening_balance_centavos=bal - net_pos,
                    closing_balance_centavos=bal,
                    available_liquidity_centavos=int(p["available_liquidity_centavos"]),
                    liquidity_utilization_bps=int(p["liquidity_utilization_bps"]),
                    status=pos_status,
                )
                session.add(pos)
                report_positions.append(
                    {
                        "institution_code": code,
                        "gross_debits_centavos": pos.gross_debits_centavos,
                        "gross_credits_centavos": pos.gross_credits_centavos,
                        "net_position_centavos": pos.net_position_centavos,
                        "closing_balance_centavos": pos.closing_balance_centavos,
                        "status": pos_status,
                    }
                )

            if cycle_status == "SETTLED":
                for tx in unsettled_txs:
                    tx.settlement_cycle_id = cycle.id
            else:
                create_system_alert(
                    session,
                    category="SETTLEMENT",
                    severity="CRITICAL",
                    title=f"MNS Cycle #{next_cycle_no} Failed — Liquidity Shortfall",
                    message=failure_reason or "Multilateral net settlement failed",
                    resource_id=str(cycle.id),
                )

            report_doc = {
                "cycle_id": str(cycle.id),
                "cycle_number": next_cycle_no,
                "currency": "943",
                "status": cycle_status,
                "tx_count": len(unsettled_txs),
                "total_gross_volume_centavos": cycle.total_gross_volume_centavos,
                "total_net_settled_centavos": cycle.total_net_settled_centavos,
                "netting_zero_sum_valid": preview["netting_zero_sum_valid"],
                "initiated_by": actor.subject,
                "generated_at": now.isoformat(),
                "failure_reason": failure_reason,
                "positions": report_positions,
            }
            cycle.report_json = report_doc
            cycle.report_signature = sign_document(
                report_doc,
                service_id=self.settings.service_id,
                master_seed=self.settings.signing_master_seed,
            )

            await record_audit_log(
                session,
                actor_id=actor.subject,
                actor_role=actor.role.value,
                action=f"MNS_CYCLE_{cycle_status}",
                resource_type="settlement_cycle",
                resource_id=str(cycle.id),
                payload_json={
                    "cycle_number": next_cycle_no,
                    "tx_count": len(unsettled_txs),
                    "gross_volume": cycle.total_gross_volume_centavos,
                    "failure_reason": failure_reason,
                },
            )
            await session.commit()

            loaded = (
                await session.execute(
                    select(SettlementCycle)
                    .where(SettlementCycle.id == cycle.id)
                    .options(selectinload(SettlementCycle.positions))
                )
            ).scalar_one()
            return loaded

    async def topup_liquidity(
        self,
        *,
        institution_code: str,
        amount_centavos: int,
        actor: Principal,
        reason: str = "Intraday RTGS liquidity top-up",
    ) -> dict[str, Any]:
        """Inject RTGS liquidity into a participant settlement account via double-entry journal."""
        if institution_code not in SETTLEMENT_ACCOUNT_BY_INSTITUTION:
            raise KeyError(f"Unknown settlement participant: {institution_code}")
        settle_no = SETTLEMENT_ACCOUNT_BY_INSTITUTION[institution_code]

        async with self.session_factory() as session:
            treasury = (
                await session.execute(
                    select(Account).where(Account.account_number == BM_RTGS_TREASURY_ACCOUNT_NUMBER)
                )
            ).scalar_one()
            settle_acct = (
                await session.execute(select(Account).where(Account.account_number == settle_no))
            ).scalar_one()
            settle_acct_id = settle_acct.id
            profile = (
                await session.execute(
                    select(ParticipantLiquidityProfile).where(
                        ParticipantLiquidityProfile.institution_code == institution_code
                    )
                )
            ).scalar_one()

            await post_balanced_journal(
                session,
                transaction_id=None,
                entry_type=JournalEntryType.LIQUIDITY_TOPUP,
                description=f"BM RTGS liquidity top-up for {institution_code}: {reason}",
                debit_account_id=treasury.id,
                credit_account_id=settle_acct_id,
                amount_centavos=amount_centavos,
            )
            profile.prefunded_limit_centavos += amount_centavos
            profile.simulate_shortfall = False

            await record_audit_log(
                session,
                actor_id=actor.subject,
                actor_role=actor.role.value,
                action="LIQUIDITY_TOPUP",
                resource_type="settlement_account",
                resource_id=settle_no,
                payload_json={
                    "institution_code": institution_code,
                    "amount_centavos": amount_centavos,
                    "reason": reason,
                },
            )
            await session.commit()
            refreshed = (
                await session.execute(
                    select(Account)
                    .where(Account.id == settle_acct_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()

        return {
            "institution_code": institution_code,
            "settlement_account_number": settle_no,
            "new_balance_centavos": refreshed.balance_centavos,
            "simulate_shortfall": False,
        }

    async def list_cycles(self, limit: int = 20) -> list[SettlementCycle]:
        async with self.session_factory() as session:
            stmt = (
                select(SettlementCycle)
                .options(selectinload(SettlementCycle.positions))
                .order_by(SettlementCycle.cycle_number.desc())
                .limit(limit)
            )
            return list((await session.execute(stmt)).scalars().all())

    async def get_signed_report(self, cycle_id: uuid.UUID) -> dict[str, Any]:
        async with self.session_factory() as session:
            cycle = (
                await session.execute(select(SettlementCycle).where(SettlementCycle.id == cycle_id))
            ).scalar_one_or_none()
            if cycle is None or cycle.report_json is None or cycle.report_signature is None:
                raise KeyError(f"Settlement report not found for cycle {cycle_id}")

        sig_valid = verify_document_signature(
            cycle.report_json,
            cycle.report_signature,
            service_id=self.settings.service_id,
            master_seed=self.settings.signing_master_seed,
        )
        return {
            "cycle_id": str(cycle.id),
            "cycle_number": cycle.cycle_number,
            "report": cycle.report_json,
            "signature_algorithm": "Ed25519",
            "signing_key_id": self.settings.service_id,
            "signature": cycle.report_signature,
            "signature_valid": sig_valid,
        }
