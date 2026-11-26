from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.registry import AdapterRegistry
from app.db.models import (
    Account,
    ReconciliationException,
    ReconciliationRun,
    SwitchTransaction,
    TransactionStatus,
)
from app.db.seed import METIX_SUSPENSE_ACCOUNT_NUMBER
from app.services.audit import create_system_alert, record_audit_log
from shared.schemas import CbsTransactionOut
from shared.security import Principal


class ReconciliationService:
    """3-Way Financial Reconciliation comparing METIX Switch vs BCI CBS vs Standard Bank CBS."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        adapter_registry: AdapterRegistry,
    ) -> None:
        self.session_factory = session_factory
        self.adapters = adapter_registry

    async def run_three_way_reconciliation(self, *, actor: Principal) -> dict[str, Any]:
        """Pull records from BCI CBS & STDB CBS and reconcile against Switch transactions."""
        bci_txs = await self.adapters.get("BCI_MZ").fetch_transactions(limit=250)
        stdb_txs = await self.adapters.get("STDB_MZ").fetch_transactions(limit=250)
        cbs_by_bank: dict[str, list[CbsTransactionOut]] = {
            "BCI_MZ": bci_txs,
            "STDB_MZ": stdb_txs,
        }

        async with self.session_factory() as session:
            switch_txs = list(
                (
                    await session.execute(
                        select(SwitchTransaction)
                        .order_by(SwitchTransaction.created_at.desc())
                        .limit(250)
                    )
                )
                .scalars()
                .all()
            )
            suspense_bal = (
                await session.execute(
                    select(Account.balance_centavos).where(
                        Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER
                    )
                )
            ).scalar_one_or_none() or 0

            # Track already-open exceptions so repeated runs do not spam identical rows
            open_existing = list(
                (
                    await session.execute(
                        select(ReconciliationException).where(
                            ReconciliationException.status.in_(["OPEN", "PENDING_ADJUSTMENT"])
                        )
                    )
                )
                .scalars()
                .all()
            )
            existing_sigs = {
                (e.exception_type, e.bank_code, e.stan, e.rrn, e.cbs_ref) for e in open_existing
            }

            run = ReconciliationRun(
                triggered_by=actor.subject,
                switch_tx_count=len(switch_txs),
                bci_tx_count=len(bci_txs),
                stdb_tx_count=len(stdb_txs),
                suspense_balance_centavos=int(suspense_bal),
                status="COMPLETED",
            )
            session.add(run)
            await session.flush()

            switch_by_key: dict[tuple[str, str], SwitchTransaction] = {
                (tx.stan, tx.rrn): tx for tx in switch_txs
            }
            cbs_index: dict[tuple[str, str, str, str], list[CbsTransactionOut]] = defaultdict(list)
            for bank_code, tx_list in cbs_by_bank.items():
                for ctx in tx_list:
                    cbs_index[(bank_code, ctx.stan, ctx.rrn, ctx.tx_type)].append(ctx)

            new_exceptions: list[ReconciliationException] = []
            matched = 0

            def _add_exc(
                *,
                exception_type: str,
                severity: str,
                bank_code: str,
                switch_tx_id: uuid.UUID | None,
                cbs_ref: str | None,
                stan: str | None,
                rrn: str | None,
                amount_centavos: int,
                description: str,
            ) -> None:
                sig = (exception_type, bank_code, stan, rrn, cbs_ref)
                if sig in existing_sigs:
                    return
                existing_sigs.add(sig)
                exc = ReconciliationException(
                    run_id=run.id,
                    exception_type=exception_type,
                    severity=severity,
                    bank_code=bank_code,
                    switch_tx_id=switch_tx_id,
                    cbs_ref=cbs_ref,
                    stan=stan,
                    rrn=rrn,
                    amount_centavos=amount_centavos,
                    description=description,
                    status="OPEN",
                )
                session.add(exc)
                new_exceptions.append(exc)

            # 1. Check Switch -> CBS expectations & incomplete/suspense states
            for stx in switch_txs:
                if stx.status in (
                    TransactionStatus.ROUTED.value,
                    TransactionStatus.TIMED_OUT.value,
                    TransactionStatus.REVERSAL_SENT.value,
                    TransactionStatus.RECON_HOLD.value,
                ):
                    sev = "CRITICAL" if stx.status == TransactionStatus.RECON_HOLD.value else "HIGH"
                    _add_exc(
                        exception_type="INCOMPLETE_TRANSFER",
                        severity=sev,
                        bank_code=stx.beneficiary_code,
                        switch_tx_id=stx.id,
                        cbs_ref=stx.originator_hold_ref,
                        stan=stx.stan,
                        rrn=stx.rrn,
                        amount_centavos=stx.amount_centavos,
                        description=(
                            f"Switch tx STAN {stx.stan} stuck in {stx.status} "
                            f"({stx.originator_code} -> {stx.beneficiary_code}); "
                            "funds held in METIX-SUSPENSE-943"
                        ),
                    )
                    continue

                if stx.status != TransactionStatus.COMPLETED.value:
                    continue

                tx_ok = True
                # Verify Originator CBS posted debit
                if stx.originator_code in cbs_by_bank:
                    orig_matches = cbs_index.get(
                        (stx.originator_code, stx.stan, stx.rrn, "OUTBOUND_DEBIT"), []
                    )
                    posted_orig = [m for m in orig_matches if m.status == "POSTED"]
                    if not posted_orig:
                        tx_ok = False
                        _add_exc(
                            exception_type="MISSING_IN_CBS",
                            severity="CRITICAL",
                            bank_code=stx.originator_code,
                            switch_tx_id=stx.id,
                            cbs_ref=orig_matches[0].cbs_ref if orig_matches else None,
                            stan=stx.stan,
                            rrn=stx.rrn,
                            amount_centavos=stx.amount_centavos,
                            description=(
                                f"Switch COMPLETED tx STAN {stx.stan} missing POSTED debit "
                                f"in {stx.originator_code} CBS"
                            ),
                        )

                # Verify Beneficiary CBS posted credit
                if stx.beneficiary_code in cbs_by_bank:
                    ben_matches = cbs_index.get(
                        (stx.beneficiary_code, stx.stan, stx.rrn, "INBOUND_CREDIT"), []
                    )
                    posted_ben = [m for m in ben_matches if m.status == "POSTED"]
                    if not posted_ben:
                        tx_ok = False
                        _add_exc(
                            exception_type="MISSING_IN_CBS",
                            severity="CRITICAL",
                            bank_code=stx.beneficiary_code,
                            switch_tx_id=stx.id,
                            cbs_ref=None,
                            stan=stx.stan,
                            rrn=stx.rrn,
                            amount_centavos=stx.amount_centavos,
                            description=(
                                f"Switch COMPLETED tx STAN {stx.stan} missing POSTED credit "
                                f"in {stx.beneficiary_code} CBS"
                            ),
                        )
                    elif posted_ben[0].amount_centavos != stx.amount_centavos:
                        tx_ok = False
                        _add_exc(
                            exception_type="AMOUNT_OR_STATUS_MISMATCH",
                            severity="CRITICAL",
                            bank_code=stx.beneficiary_code,
                            switch_tx_id=stx.id,
                            cbs_ref=posted_ben[0].cbs_ref,
                            stan=stx.stan,
                            rrn=stx.rrn,
                            amount_centavos=abs(
                                stx.amount_centavos - posted_ben[0].amount_centavos
                            ),
                            description=(
                                f"Amount mismatch STAN {stx.stan}: Switch={stx.amount_centavos}ct "
                                f"vs {stx.beneficiary_code}={posted_ben[0].amount_centavos}ct"
                            ),
                        )

                if tx_ok:
                    matched += 1

            # 2. Check CBS -> Switch (Orphaned CBS postings, Duplicate postings, Stale CBS holds)
            for (bank_code, stan, rrn, tx_type), items in cbs_index.items():
                if tx_type in ("ON_US_DEBIT", "ON_US_CREDIT"):
                    # Intrabank on-us book transfers settle directly inside CBS without METIX/SPIM
                    continue
                posted_items = [i for i in items if i.status == "POSTED"]
                if len(posted_items) > 1:
                    _add_exc(
                        exception_type="DUPLICATE_POSTING",
                        severity="CRITICAL",
                        bank_code=bank_code,
                        switch_tx_id=posted_items[0].switch_tx_id,
                        cbs_ref=posted_items[-1].cbs_ref,
                        stan=stan,
                        rrn=rrn,
                        amount_centavos=posted_items[-1].amount_centavos,
                        description=(
                            f"Duplicate {tx_type} postings ({len(posted_items)}x) detected in "
                            f"{bank_code} CBS for STAN {stan} / RRN {rrn}"
                        ),
                    )

                for item in items:
                    stx = switch_by_key.get((stan, rrn))
                    if stx is None and item.status in ("POSTED", "HELD"):
                        exc_type = (
                            "INCOMPLETE_TRANSFER" if item.status == "HELD" else "MISSING_IN_SWITCH"
                        )
                        _add_exc(
                            exception_type=exc_type,
                            severity="HIGH",
                            bank_code=bank_code,
                            switch_tx_id=item.switch_tx_id,
                            cbs_ref=item.cbs_ref,
                            stan=stan,
                            rrn=rrn,
                            amount_centavos=item.amount_centavos,
                            description=(
                                f"{bank_code} CBS record {item.cbs_ref} ({item.tx_type}, "
                                f"{item.status}) has no matching METIX Switch transaction "
                                f"(STAN {stan})"
                            ),
                        )
                    elif (
                        stx is not None
                        and item.status == "HELD"
                        and stx.status
                        in (
                            TransactionStatus.COMPLETED.value,
                            TransactionStatus.DECLINED.value,
                            TransactionStatus.REVERSED.value,
                        )
                    ):
                        _add_exc(
                            exception_type="INCOMPLETE_TRANSFER",
                            severity="HIGH",
                            bank_code=bank_code,
                            switch_tx_id=stx.id,
                            cbs_ref=item.cbs_ref,
                            stan=stan,
                            rrn=rrn,
                            amount_centavos=item.amount_centavos,
                            description=(
                                f"Stale CBS hold {item.cbs_ref} in {bank_code} while Switch tx "
                                f"STAN {stan} is already {stx.status}"
                            ),
                        )

            # 3. Suspense aging check
            if int(suspense_bal) > 0:
                aged_count = sum(
                    1
                    for tx in switch_txs
                    if tx.status
                    in (TransactionStatus.RECON_HOLD.value, TransactionStatus.TIMED_OUT.value)
                )
                if aged_count > 0:
                    _add_exc(
                        exception_type="SUSPENSE_AGING",
                        severity="CRITICAL",
                        bank_code="METIX_SWITCH",
                        switch_tx_id=None,
                        cbs_ref=METIX_SUSPENSE_ACCOUNT_NUMBER,
                        stan=None,
                        rrn=None,
                        amount_centavos=int(suspense_bal),
                        description=(
                            f"METIX-SUSPENSE-943 holds {int(suspense_bal) / 100:,.2f} MT across "
                            f"{aged_count} RECON_HOLD/TIMED_OUT transaction(s)"
                        ),
                    )

            run.matched_count = matched
            run.exception_count = len(new_exceptions)

            if new_exceptions:
                create_system_alert(
                    session,
                    category="RECONCILIATION",
                    severity="WARNING",
                    title=f"3-Way Recon Found {len(new_exceptions)} Exception(s)",
                    message=(
                        f"Reconciled Switch ({len(switch_txs)}), BCI ({len(bci_txs)}), and "
                        f"STDB ({len(stdb_txs)}): {len(new_exceptions)} new exception(s) opened."
                    ),
                    resource_id=str(run.id),
                )

            await record_audit_log(
                session,
                actor_id=actor.subject,
                actor_role=actor.role.value,
                action="RECONCILIATION_RUN",
                resource_type="reconciliation_run",
                resource_id=str(run.id),
                payload_json={
                    "switch_txs": len(switch_txs),
                    "bci_txs": len(bci_txs),
                    "stdb_txs": len(stdb_txs),
                    "matched": matched,
                    "new_exceptions": len(new_exceptions),
                },
            )
            await session.commit()

            all_open = list(
                (
                    await session.execute(
                        select(ReconciliationException)
                        .order_by(ReconciliationException.created_at.desc())
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "run": {
                    "id": str(run.id),
                    "triggered_by": run.triggered_by,
                    "switch_tx_count": run.switch_tx_count,
                    "bci_tx_count": run.bci_tx_count,
                    "stdb_tx_count": run.stdb_tx_count,
                    "matched_count": run.matched_count,
                    "new_exception_count": run.exception_count,
                    "suspense_balance_centavos": run.suspense_balance_centavos,
                    "created_at": run.created_at.isoformat()
                    if run.created_at
                    else datetime.now(UTC).isoformat(),
                },
                "exceptions": [self._serialize_exception(e) for e in all_open],
            }

    @staticmethod
    def _serialize_exception(exc: ReconciliationException) -> dict[str, Any]:
        return {
            "id": str(exc.id),
            "run_id": str(exc.run_id),
            "exception_type": exc.exception_type,
            "severity": exc.severity,
            "bank_code": exc.bank_code,
            "switch_tx_id": str(exc.switch_tx_id) if exc.switch_tx_id else None,
            "cbs_ref": exc.cbs_ref,
            "stan": exc.stan,
            "rrn": exc.rrn,
            "amount_centavos": exc.amount_centavos,
            "description": exc.description,
            "status": exc.status,
            "resolution_notes": exc.resolution_notes,
            "resolved_at": exc.resolved_at.isoformat() if exc.resolved_at else None,
            "created_at": exc.created_at.isoformat(),
        }

    async def list_exceptions(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ReconciliationException)
                        .order_by(ReconciliationException.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [self._serialize_exception(r) for r in rows]

    async def get_suspense_breakdown(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            suspense_acct = (
                await session.execute(
                    select(Account).where(Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER)
                )
            ).scalar_one_or_none()
            uncleared_txs = list(
                (
                    await session.execute(
                        select(SwitchTransaction)
                        .where(
                            SwitchTransaction.status.in_(
                                [
                                    TransactionStatus.ROUTED.value,
                                    TransactionStatus.TIMED_OUT.value,
                                    TransactionStatus.REVERSAL_SENT.value,
                                    TransactionStatus.RECON_HOLD.value,
                                ]
                            )
                        )
                        .order_by(SwitchTransaction.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
        return {
            "suspense_account_number": METIX_SUSPENSE_ACCOUNT_NUMBER,
            "balance_centavos": suspense_acct.balance_centavos if suspense_acct else 0,
            "version": suspense_acct.version if suspense_acct else 1,
            "uncleared_transactions": [
                {
                    "id": str(tx.id),
                    "stan": tx.stan,
                    "rrn": tx.rrn,
                    "originator_code": tx.originator_code,
                    "beneficiary_code": tx.beneficiary_code,
                    "amount_centavos": tx.amount_centavos,
                    "status": tx.status,
                    "failure_reason": tx.failure_reason,
                    "created_at": tx.created_at.isoformat(),
                }
                for tx in uncleared_txs
            ],
        }
