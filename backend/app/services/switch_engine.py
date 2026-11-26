from __future__ import annotations

import logging
import secrets
import string
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.adapters.registry import AdapterRegistry
from app.config import Settings, get_settings
from app.db.models import (
    Account,
    FraudCase,
    FraudInvestigationEvent,
    Iso8583MessageLog,
    JournalEntry,
    JournalEntryType,
    MessageDirection,
    SwitchTransaction,
    TransactionStatus,
    validate_transition,
)
from app.db.seed import (
    METIX_SUSPENSE_ACCOUNT_NUMBER,
    SETTLEMENT_ACCOUNT_BY_INSTITUTION,
)
from app.services.audit import create_system_alert, record_audit_log
from app.services.fraud import FraudEngine
from app.services.ledger import InsufficientFundsError, post_balanced_journal
from app.services.redis_guard import RedisGuard
from shared.iso8583.codec import build_0200, build_0210, encode_raw_iso
from shared.iso8583.models import Iso8583Message, format_de7
from shared.schemas import (
    CbsDebitAuthorizeRequest,
    CbsDebitCaptureRequest,
    CbsDebitReleaseRequest,
)

logger = logging.getLogger(__name__)


@dataclass
class TransferRequest:
    idempotency_key: str
    originator_account_number: str
    beneficiary_account_number: str
    amount_centavos: int
    stan: str | None = None
    rrn: str | None = None
    actor_id: str = "system"
    actor_role: str = "SERVICE_ADAPTER"
    bypass_fraud_check: bool = False


@dataclass
class TransferResult:
    transaction: SwitchTransaction
    idempotent_replay: bool


def _generate_rrn(stan: str, now: datetime) -> str:
    """Build 12-char RRN: YDDDHH + 6-digit STAN."""
    y = now.strftime("%y")[-1]
    ddd = now.strftime("%j")
    hh = now.strftime("%H")
    return f"{y}{ddd}{hh}{stan}"


def generate_auth_id() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


def _record_iso_log(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    msg: Iso8583Message,
    direction: MessageDirection,
    signature_key_id: str | None = "switch-api",
    signature_verified: bool = True,
) -> Iso8583MessageLog:
    raw = encode_raw_iso(msg)
    log_row = Iso8583MessageLog(
        transaction_id=transaction_id,
        mti=msg.mti.value,
        direction=direction.value,
        stan=msg.de11,
        rrn=msg.de37,
        response_code=msg.de39,
        payload_json=msg.model_dump(mode="json"),
        raw_iso_snippet=raw,
        signature_key_id=signature_key_id,
        signature_verified=signature_verified,
    )
    session.add(log_row)
    return log_row


async def load_transaction_full(
    session: AsyncSession, transaction_id: uuid.UUID
) -> SwitchTransaction:
    stmt = (
        select(SwitchTransaction)
        .where(SwitchTransaction.id == transaction_id)
        .options(
            selectinload(SwitchTransaction.iso_messages),
            selectinload(SwitchTransaction.journal_entries).selectinload(JournalEntry.postings),
        )
        .execution_options(populate_existing=True)
    )
    res = await session.execute(stmt)
    return res.scalar_one()


class SwitchEngine:
    """METIX / SIMOrede Central Switch Router coordinating independent bank CBS adapters."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: redis.Redis,
        adapter_registry: AdapterRegistry,
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.redis_client = redis_client
        self.redis_guard = RedisGuard(redis_client, settings)
        self.fraud_engine = FraudEngine(redis_client, settings)
        self.adapters = adapter_registry
        self.settings = settings or get_settings()

    def _daily_limit_key_ref(self, bank_code: str, account_number: str) -> str:
        return f"{bank_code}:{account_number}"

    async def _resolve_existing_idempotent_tx(
        self, idempotency_key: str
    ) -> SwitchTransaction | None:
        bound_id = await self.redis_guard.get_bound_transaction_id(
            idempotency_key, wait_seconds=3.0
        )
        async with self.session_factory() as session:
            if bound_id is not None:
                return await load_transaction_full(session, bound_id)
            res = await session.execute(
                select(SwitchTransaction).where(
                    SwitchTransaction.idempotency_key == idempotency_key
                )
            )
            tx = res.scalar_one_or_none()
            if tx is not None:
                return await load_transaction_full(session, tx.id)
        return None

    async def initiate_transfer(self, req: TransferRequest) -> TransferResult:
        started_perf = time.perf_counter()
        if req.amount_centavos <= 0:
            raise ValueError("amount_centavos must be a positive integer")
        if req.originator_account_number == req.beneficiary_account_number:
            raise ValueError("Originator and beneficiary accounts must be distinct")

        # 1. Atomic Redis idempotency lock (SET NX EX)
        acquired = await self.redis_guard.acquire_idempotency_lock(req.idempotency_key)
        if not acquired:
            existing = await self._resolve_existing_idempotent_tx(req.idempotency_key)
            if existing is not None:
                return TransferResult(transaction=existing, idempotent_replay=True)
            raise RuntimeError(
                f"Duplicate request for idempotency_key={req.idempotency_key} is still initializing"
            )

        now = datetime.now(UTC)
        stan = req.stan or await self.redis_guard.next_stan()
        rrn = req.rrn or _generate_rrn(stan, now)
        de7 = format_de7(now)

        stan_rrn_ok = await self.redis_guard.acquire_stan_rrn_lock(stan, rrn, req.idempotency_key)
        if not stan_rrn_ok:
            await self.redis_guard.release_idempotency_lock(req.idempotency_key)
            async with self.session_factory() as session:
                res = await session.execute(
                    select(SwitchTransaction).where(
                        SwitchTransaction.stan == stan, SwitchTransaction.rrn == rrn
                    )
                )
                existing_by_stan = res.scalar_one_or_none()
                if existing_by_stan is not None:
                    full = await load_transaction_full(session, existing_by_stan.id)
                    return TransferResult(transaction=full, idempotent_replay=True)
            raise ValueError(f"Duplicate STAN/RRN combination: {stan}/{rrn}")

        tx_id: uuid.UUID | None = None
        iso_0200: Iso8583Message | None = None
        orig_bank_code: str | None = None
        ben_bank_code: str | None = None

        try:
            # 2. Resolve Originator & Beneficiary institutions via AdapterRegistry
            orig_bank_code, orig_cbs_acct = await self.adapters.lookup_account(
                req.originator_account_number
            )
            ben_bank_code = self.adapters.resolve_bank_code_for_account(
                req.beneficiary_account_number
            )
            if orig_bank_code == ben_bank_code:
                raise ValueError(
                    f"Same-bank transfers within {orig_bank_code} do not require METIX/SPIM "
                    "and must execute directly inside the bank's Core Banking System"
                )

            orig_settle_no = SETTLEMENT_ACCOUNT_BY_INSTITUTION[orig_bank_code]
            ben_settle_no = SETTLEMENT_ACCOUNT_BY_INSTITUTION[ben_bank_code]

            async with self.session_factory() as session:
                rows = (
                    await session.execute(
                        select(Account).where(
                            Account.account_number.in_(
                                [orig_settle_no, ben_settle_no, METIX_SUSPENSE_ACCOUNT_NUMBER]
                            )
                        )
                    )
                ).scalars()
                accts = {a.account_number: a for a in rows}
                if (
                    orig_settle_no not in accts
                    or ben_settle_no not in accts
                    or METIX_SUSPENSE_ACCOUNT_NUMBER not in accts
                ):
                    raise RuntimeError("METIX Switch settlement/suspense accounts are not seeded")

                orig_settle_acct = accts[orig_settle_no]
                ben_settle_acct = accts[ben_settle_no]

                iso_0200 = build_0200(
                    originator_account=req.originator_account_number,
                    originator_code=orig_bank_code,
                    beneficiary_account=req.beneficiary_account_number,
                    beneficiary_code=ben_bank_code,
                    amount_centavos=req.amount_centavos,
                    stan=stan,
                    rrn=rrn,
                    de7=de7,
                )

                sla_deadline = now + timedelta(seconds=self.settings.spim_sla_seconds)
                tx = SwitchTransaction(
                    idempotency_key=req.idempotency_key,
                    stan=stan,
                    rrn=rrn,
                    transmission_datetime=de7,
                    originator_code=orig_bank_code,
                    beneficiary_code=ben_bank_code,
                    originator_account_id=orig_settle_acct.id,
                    beneficiary_account_id=ben_settle_acct.id,
                    originator_account_number=req.originator_account_number,
                    beneficiary_account_number=req.beneficiary_account_number,
                    amount_centavos=req.amount_centavos,
                    currency=self.settings.currency_code,
                    status=TransactionStatus.INITIATED.value,
                    sla_deadline_at=sla_deadline,
                    signature_verified=True,
                )
                session.add(tx)
                await session.flush()
                tx_id = tx.id

                _record_iso_log(
                    session,
                    transaction_id=tx.id,
                    msg=iso_0200,
                    direction=MessageDirection.INBOUND,
                    signature_key_id=f"{orig_bank_code.lower()}-adapter",
                )

                # 3. Pre-route Fraud & Velocity Detection
                if not req.bypass_fraud_check:
                    daily_ref = self._daily_limit_key_ref(
                        orig_bank_code, req.originator_account_number
                    )
                    used_before = await self.redis_guard.get_daily_usage(daily_ref)
                    fraud_eval = await self.fraud_engine.evaluate_transfer(
                        originator_code=orig_bank_code,
                        originator_account=req.originator_account_number,
                        originator_tier=orig_cbs_acct.account_tier,
                        beneficiary_code=ben_bank_code,
                        beneficiary_account=req.beneficiary_account_number,
                        amount_centavos=req.amount_centavos,
                        daily_used_before_centavos=used_before,
                    )
                    tx.fraud_score = fraud_eval.risk_score

                    if fraud_eval.decision == "BLOCK":
                        tx.fraud_status = "BLOCKED"
                        validate_transition(
                            TransactionStatus(tx.status), TransactionStatus.DECLINED
                        )
                        tx.status = TransactionStatus.DECLINED.value
                        tx.response_code = "59"
                        tx.failure_reason = (
                            f"Blocked by Fraud Engine (risk score {fraud_eval.risk_score}/100)"
                        )
                        tx.processing_latency_ms = int((time.perf_counter() - started_perf) * 1000)

                        fcase = FraudCase(
                            transaction_id=tx.id,
                            originator_code=orig_bank_code,
                            originator_account_number=req.originator_account_number,
                            beneficiary_code=ben_bank_code,
                            beneficiary_account_number=req.beneficiary_account_number,
                            amount_centavos=req.amount_centavos,
                            risk_score=fraud_eval.risk_score,
                            triggered_rules=fraud_eval.triggered_rules,
                            status="AUTO_BLOCKED",
                            decision_notes="Automatic pre-route block (score >= 80)",
                        )
                        session.add(fcase)
                        await session.flush()
                        session.add(
                            FraudInvestigationEvent(
                                fraud_case_id=fcase.id,
                                actor_id="fraud-engine",
                                actor_role="SYSTEM",
                                action="AUTO_BLOCKED",
                                notes=tx.failure_reason,
                                metadata_json={"rules": fraud_eval.triggered_rules},
                            )
                        )
                        create_system_alert(
                            session,
                            category="FRAUD",
                            severity="CRITICAL",
                            title=f"High-Risk Transfer Blocked (Score {fraud_eval.risk_score})",
                            message=(
                                f"STAN {stan}: {orig_bank_code}:{req.originator_account_number} -> "
                                f"{ben_bank_code}:{req.beneficiary_account_number} "
                                "blocked (DE 39=59)."
                            ),
                            resource_id=str(tx.id),
                        )
                        _record_iso_log(
                            session,
                            transaction_id=tx.id,
                            msg=build_0210(iso_0200, response_code="59"),
                            direction=MessageDirection.OUTBOUND,
                        )
                        await record_audit_log(
                            session,
                            actor_id="fraud-engine",
                            actor_role="SYSTEM",
                            action="FRAUD_AUTO_BLOCK",
                            resource_type="switch_transaction",
                            resource_id=str(tx.id),
                            payload_json={
                                "stan": stan,
                                "risk_score": fraud_eval.risk_score,
                                "rules": fraud_eval.triggered_rules,
                            },
                        )
                        await session.commit()
                        await self.redis_guard.bind_transaction_id(req.idempotency_key, tx.id)
                        full = await load_transaction_full(session, tx.id)
                        return TransferResult(transaction=full, idempotent_replay=False)

                    if fraud_eval.decision == "REVIEW_QUEUE":
                        tx.fraud_status = "PENDING_REVIEW"
                        tx.failure_reason = (
                            f"Held in Fraud Review Queue (risk score {fraud_eval.risk_score}/100)"
                        )
                        tx.processing_latency_ms = int((time.perf_counter() - started_perf) * 1000)

                        fcase = FraudCase(
                            transaction_id=tx.id,
                            originator_code=orig_bank_code,
                            originator_account_number=req.originator_account_number,
                            beneficiary_code=ben_bank_code,
                            beneficiary_account_number=req.beneficiary_account_number,
                            amount_centavos=req.amount_centavos,
                            risk_score=fraud_eval.risk_score,
                            triggered_rules=fraud_eval.triggered_rules,
                            status="PENDING_REVIEW",
                        )
                        session.add(fcase)
                        await session.flush()
                        session.add(
                            FraudInvestigationEvent(
                                fraud_case_id=fcase.id,
                                actor_id="fraud-engine",
                                actor_role="SYSTEM",
                                action="FLAGGED_FOR_REVIEW",
                                notes=tx.failure_reason,
                                metadata_json={"rules": fraud_eval.triggered_rules},
                            )
                        )
                        create_system_alert(
                            session,
                            category="FRAUD",
                            severity="WARNING",
                            title=(
                                f"Transfer Flagged for Fraud Review (Score {fraud_eval.risk_score})"
                            ),
                            message=(
                                f"STAN {stan}: {orig_bank_code}:{req.originator_account_number} -> "
                                f"{ben_bank_code}:{req.beneficiary_account_number} "
                                "queued for analyst review."
                            ),
                            resource_id=str(tx.id),
                        )
                        await record_audit_log(
                            session,
                            actor_id="fraud-engine",
                            actor_role="SYSTEM",
                            action="FRAUD_QUEUED_FOR_REVIEW",
                            resource_type="switch_transaction",
                            resource_id=str(tx.id),
                            payload_json={
                                "stan": stan,
                                "risk_score": fraud_eval.risk_score,
                                "rules": fraud_eval.triggered_rules,
                            },
                        )
                        await session.commit()
                        await self.redis_guard.bind_transaction_id(req.idempotency_key, tx.id)
                        full = await load_transaction_full(session, tx.id)
                        return TransferResult(transaction=full, idempotent_replay=False)

                await session.commit()
                await self.redis_guard.bind_transaction_id(req.idempotency_key, tx.id)

        except Exception:
            if tx_id is None:
                await self.redis_guard.release_idempotency_lock(req.idempotency_key)
            raise

        assert tx_id is not None and iso_0200 is not None
        assert orig_bank_code is not None and ben_bank_code is not None

        return await self._execute_routing_and_settlement(
            tx_id=tx_id,
            iso_0200=iso_0200,
            orig_tier=orig_cbs_acct.account_tier,
            started_perf=started_perf,
        )

    async def resume_reviewed_transfer(
        self, transaction_id: uuid.UUID, *, actor_id: str, actor_role: str
    ) -> SwitchTransaction:
        """Resume an INITIATED transfer after a Fraud Analyst releases it from the review queue."""
        started_perf = time.perf_counter()
        async with self.session_factory() as session:
            tx = (
                await session.execute(
                    select(SwitchTransaction)
                    .where(SwitchTransaction.id == transaction_id)
                    .with_for_update()
                )
            ).scalar_one()
            if (
                tx.status != TransactionStatus.INITIATED.value
                or tx.fraud_status != "PENDING_REVIEW"
            ):
                raise ValueError(
                    f"Transaction {transaction_id} is not awaiting fraud review "
                    f"(status={tx.status}, fraud_status={tx.fraud_status})"
                )
            tx.fraud_status = "RELEASED"
            tx.failure_reason = None
            await record_audit_log(
                session,
                actor_id=actor_id,
                actor_role=actor_role,
                action="FRAUD_CASE_RELEASED",
                resource_type="switch_transaction",
                resource_id=str(tx.id),
                payload_json={"stan": tx.stan},
            )
            await session.commit()

            orig_acct = await self.adapters.get(tx.originator_code).get_balance(
                tx.originator_account_number
            )
            tier = orig_acct.account_tier if orig_acct else "INDIVIDUAL"
            iso_0200 = build_0200(
                originator_account=tx.originator_account_number,
                originator_code=tx.originator_code,
                beneficiary_account=tx.beneficiary_account_number,
                beneficiary_code=tx.beneficiary_code,
                amount_centavos=tx.amount_centavos,
                stan=tx.stan,
                rrn=tx.rrn,
                de7=tx.transmission_datetime,
            )

        res = await self._execute_routing_and_settlement(
            tx_id=transaction_id,
            iso_0200=iso_0200,
            orig_tier=tier,
            started_perf=started_perf,
        )
        return res.transaction

    async def reject_reviewed_transfer(
        self,
        transaction_id: uuid.UUID,
        *,
        actor_id: str,
        actor_role: str,
        notes: str,
    ) -> SwitchTransaction:
        """Confirm fraud on an INITIATED transfer -> DECLINED (DE 39 = 59)."""
        async with self.session_factory() as session:
            tx = (
                await session.execute(
                    select(SwitchTransaction)
                    .where(SwitchTransaction.id == transaction_id)
                    .with_for_update()
                )
            ).scalar_one()
            if tx.status != TransactionStatus.INITIATED.value:
                raise ValueError(f"Cannot reject transaction in status {tx.status}")
            validate_transition(TransactionStatus(tx.status), TransactionStatus.DECLINED)
            tx.status = TransactionStatus.DECLINED.value
            tx.fraud_status = "CONFIRMED_FRAUD"
            tx.response_code = "59"
            tx.failure_reason = f"Confirmed fraud by {actor_id}: {notes}"
            iso_0200 = build_0200(
                originator_account=tx.originator_account_number,
                originator_code=tx.originator_code,
                beneficiary_account=tx.beneficiary_account_number,
                beneficiary_code=tx.beneficiary_code,
                amount_centavos=tx.amount_centavos,
                stan=tx.stan,
                rrn=tx.rrn,
                de7=tx.transmission_datetime,
            )
            _record_iso_log(
                session,
                transaction_id=tx.id,
                msg=build_0210(iso_0200, response_code="59"),
                direction=MessageDirection.OUTBOUND,
            )
            await record_audit_log(
                session,
                actor_id=actor_id,
                actor_role=actor_role,
                action="FRAUD_CASE_CONFIRMED",
                resource_type="switch_transaction",
                resource_id=str(tx.id),
                payload_json={"stan": tx.stan, "notes": notes},
            )
            await session.commit()
            return await load_transaction_full(session, tx.id)

    async def _execute_routing_and_settlement(
        self,
        *,
        tx_id: uuid.UUID,
        iso_0200: Iso8583Message,
        orig_tier: str,
        started_perf: float,
    ) -> TransferResult:
        # Step A: Daily limit + Originator CBS Debit Authorization + Leg 1 Clearing Hold
        async with self.session_factory() as session:
            tx = (
                await session.execute(
                    select(SwitchTransaction).where(SwitchTransaction.id == tx_id).with_for_update()
                )
            ).scalar_one()
            orig_code = tx.originator_code
            ben_code = tx.beneficiary_code
            orig_acct_no = tx.originator_account_number
            ben_acct_no = tx.beneficiary_account_number
            amount = tx.amount_centavos
            idemp_key = tx.idempotency_key
            stan = tx.stan
            rrn = tx.rrn

            daily_ref = self._daily_limit_key_ref(orig_code, orig_acct_no)
            allowed, used, cap = await self.redis_guard.reserve_daily_limit(
                daily_ref, orig_tier, amount
            )
            if not allowed:
                validate_transition(TransactionStatus(tx.status), TransactionStatus.DECLINED)
                tx.status = TransactionStatus.DECLINED.value
                tx.response_code = "61"
                tx.failure_reason = (
                    f"Exceeds Notice No. 1/GBM/2026 {orig_tier} daily limit "
                    f"({cap / 100:,.2f} MT; used {used / 100:,.2f} MT)"
                )
                tx.processing_latency_ms = int((time.perf_counter() - started_perf) * 1000)
                _record_iso_log(
                    session,
                    transaction_id=tx.id,
                    msg=build_0210(iso_0200, response_code="61"),
                    direction=MessageDirection.OUTBOUND,
                )
                await session.commit()
                full = await load_transaction_full(session, tx.id)
                return TransferResult(transaction=full, idempotent_replay=False)

        # Call Originator CBS Adapter OUTSIDE DB lock to earmark customer available_balance
        orig_adapter = self.adapters.get(orig_code)
        hold_ref: str | None = None
        if orig_adapter.supports_originator_debit:
            auth_resp = await orig_adapter.authorize_debit(
                CbsDebitAuthorizeRequest(
                    switch_tx_id=tx_id,
                    idempotency_key=idemp_key,
                    stan=stan,
                    rrn=rrn,
                    account_number=orig_acct_no,
                    counterparty_bank=ben_code,
                    counterparty_account=ben_acct_no,
                    amount_centavos=amount,
                )
            )
            if not auth_resp.approved:
                await self.redis_guard.release_daily_limit(daily_ref, amount)
                async with self.session_factory() as session:
                    tx = (
                        await session.execute(
                            select(SwitchTransaction)
                            .where(SwitchTransaction.id == tx_id)
                            .with_for_update()
                        )
                    ).scalar_one()
                    validate_transition(TransactionStatus(tx.status), TransactionStatus.DECLINED)
                    tx.status = TransactionStatus.DECLINED.value
                    tx.response_code = auth_resp.response_code
                    tx.failure_reason = auth_resp.reason or (
                        f"Originator CBS {orig_code} declined debit "
                        f"(DE 39={auth_resp.response_code})"
                    )
                    tx.processing_latency_ms = int((time.perf_counter() - started_perf) * 1000)
                    _record_iso_log(
                        session,
                        transaction_id=tx.id,
                        msg=build_0210(iso_0200, response_code=auth_resp.response_code),
                        direction=MessageDirection.OUTBOUND,
                    )
                    await session.commit()
                    full = await load_transaction_full(session, tx.id)
                    return TransferResult(transaction=full, idempotent_replay=False)
            hold_ref = auth_resp.hold_ref

        # Switch Leg 1 Clearing Hold: DEBIT Originator Settlement Account -> CREDIT METIX_SUSPENSE
        async with self.session_factory() as session:
            tx = (
                await session.execute(
                    select(SwitchTransaction).where(SwitchTransaction.id == tx_id).with_for_update()
                )
            ).scalar_one()
            suspense_acct = (
                await session.execute(
                    select(Account).where(Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER)
                )
            ).scalar_one()
            tx.originator_hold_ref = hold_ref

            try:
                await post_balanced_journal(
                    session,
                    transaction_id=tx.id,
                    entry_type=JournalEntryType.CLEARING_HOLD,
                    description=(
                        f"Clearing hold for STAN {stan} ({orig_code}:{orig_acct_no} -> "
                        f"{ben_code}:{ben_acct_no})"
                    ),
                    debit_account_id=tx.originator_account_id,
                    credit_account_id=suspense_acct.id,
                    amount_centavos=amount,
                )
            except InsufficientFundsError as exc:
                await self.redis_guard.release_daily_limit(daily_ref, amount)
                if hold_ref and orig_adapter.supports_originator_debit:
                    await orig_adapter.release_debit(
                        CbsDebitReleaseRequest(
                            switch_tx_id=tx.id,
                            hold_ref=hold_ref,
                            stan=stan,
                            rrn=rrn,
                            reason="Switch settlement account liquidity shortfall",
                        )
                    )
                validate_transition(TransactionStatus(tx.status), TransactionStatus.DECLINED)
                tx.status = TransactionStatus.DECLINED.value
                tx.response_code = "51"
                tx.failure_reason = str(exc)
                tx.processing_latency_ms = int((time.perf_counter() - started_perf) * 1000)
                _record_iso_log(
                    session,
                    transaction_id=tx.id,
                    msg=build_0210(iso_0200, response_code="51"),
                    direction=MessageDirection.OUTBOUND,
                )
                await session.commit()
                full = await load_transaction_full(session, tx.id)
                return TransferResult(transaction=full, idempotent_replay=False)

            validate_transition(TransactionStatus(tx.status), TransactionStatus.ROUTED)
            tx.status = TransactionStatus.ROUTED.value
            tx.sla_deadline_at = datetime.now(UTC) + timedelta(
                seconds=self.settings.spim_sla_seconds
            )
            _record_iso_log(
                session,
                transaction_id=tx.id,
                msg=iso_0200,
                direction=MessageDirection.OUTBOUND,
                signature_key_id=self.settings.service_id,
            )
            await session.commit()

        # Step B: Dispatch ISO 8583 0200 to Beneficiary Integration Adapter
        ben_adapter = self.adapters.get(ben_code)
        transport_result = await ben_adapter.send_iso_message(
            iso_0200,
            timeout_seconds=float(self.settings.spim_sla_seconds),
            switch_tx_id=tx_id,
        )

        if (
            transport_result.dropped
            or transport_result.timed_out
            or transport_result.response_message is None
        ):
            # Transaction remains ROUTED; SLA sweeper will dispatch 0420 and release hold on expiry
            async with self.session_factory() as session:
                full = await load_transaction_full(session, tx_id)
                return TransferResult(transaction=full, idempotent_replay=False)

        # Step C: Process 0210 under row-level lock
        resp_0210 = transport_result.response_message
        should_capture = False
        should_release = False
        captured_hold_ref: str | None = None

        async with self.session_factory() as session:
            locked_tx = (
                await session.execute(
                    select(SwitchTransaction).where(SwitchTransaction.id == tx_id).with_for_update()
                )
            ).scalar_one()

            _record_iso_log(
                session,
                transaction_id=locked_tx.id,
                msg=resp_0210,
                direction=MessageDirection.INBOUND,
                signature_key_id=f"{ben_code.lower()}-cbs",
                signature_verified=transport_result.signature_verified,
            )

            if locked_tx.status != TransactionStatus.ROUTED.value:
                logger.warning(
                    "Ignoring late 0210 for tx=%s already in status=%s",
                    locked_tx.id,
                    locked_tx.status,
                )
                await session.commit()
                full = await load_transaction_full(session, locked_tx.id)
                return TransferResult(transaction=full, idempotent_replay=False)

            suspense_acct = (
                await session.execute(
                    select(Account).where(Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER)
                )
            ).scalar_one()
            captured_hold_ref = locked_tx.originator_hold_ref

            if resp_0210.de39 == "00":
                await post_balanced_journal(
                    session,
                    transaction_id=locked_tx.id,
                    entry_type=JournalEntryType.SETTLEMENT,
                    description=(
                        f"Final clearing settlement for STAN {locked_tx.stan} to "
                        f"{locked_tx.beneficiary_code}"
                    ),
                    debit_account_id=suspense_acct.id,
                    credit_account_id=locked_tx.beneficiary_account_id,
                    amount_centavos=locked_tx.amount_centavos,
                )
                validate_transition(
                    TransactionStatus(locked_tx.status), TransactionStatus.COMPLETED
                )
                locked_tx.status = TransactionStatus.COMPLETED.value
                locked_tx.response_code = "00"
                locked_tx.auth_id = resp_0210.de38
                locked_tx.processing_latency_ms = int((time.perf_counter() - started_perf) * 1000)
                should_capture = True
            else:
                await post_balanced_journal(
                    session,
                    transaction_id=locked_tx.id,
                    entry_type=JournalEntryType.REVERSAL,
                    description=(
                        f"Decline reversal for STAN {locked_tx.stan} (DE 39 = {resp_0210.de39})"
                    ),
                    debit_account_id=suspense_acct.id,
                    credit_account_id=locked_tx.originator_account_id,
                    amount_centavos=locked_tx.amount_centavos,
                )
                await self.redis_guard.release_daily_limit(daily_ref, locked_tx.amount_centavos)
                validate_transition(TransactionStatus(locked_tx.status), TransactionStatus.DECLINED)
                locked_tx.status = TransactionStatus.DECLINED.value
                locked_tx.response_code = resp_0210.de39
                locked_tx.failure_reason = (
                    f"Beneficiary {ben_code} rejected request (DE 39 = {resp_0210.de39})"
                )
                locked_tx.processing_latency_ms = int((time.perf_counter() - started_perf) * 1000)
                should_release = True

            _record_iso_log(
                session,
                transaction_id=locked_tx.id,
                msg=resp_0210,
                direction=MessageDirection.OUTBOUND,
                signature_key_id=self.settings.service_id,
            )
            await session.commit()

        # Finalize Originator CBS hold (capture on 00, release on decline) outside DB transaction
        if captured_hold_ref and orig_adapter.supports_originator_debit:
            try:
                if should_capture:
                    await orig_adapter.capture_debit(
                        CbsDebitCaptureRequest(
                            switch_tx_id=tx_id,
                            hold_ref=captured_hold_ref,
                            stan=stan,
                            rrn=rrn,
                        )
                    )
                elif should_release:
                    await orig_adapter.release_debit(
                        CbsDebitReleaseRequest(
                            switch_tx_id=tx_id,
                            hold_ref=captured_hold_ref,
                            stan=stan,
                            rrn=rrn,
                            reason=f"Beneficiary declined (DE 39={resp_0210.de39})",
                        )
                    )
            except Exception as exc:
                logger.error(
                    "Originator CBS %s hold finalization failed for tx=%s hold=%s: %s",
                    orig_code,
                    tx_id,
                    captured_hold_ref,
                    exc,
                )

        async with self.session_factory() as session:
            full = await load_transaction_full(session, tx_id)
            return TransferResult(transaction=full, idempotent_replay=False)
