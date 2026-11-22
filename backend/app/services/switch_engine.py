from __future__ import annotations

import logging
import secrets
import string
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.db.models import (
    Account,
    Institution,
    Iso8583MessageLog,
    JournalEntry,
    JournalEntryType,
    MessageDirection,
    SwitchTransaction,
    TransactionStatus,
    validate_transition,
)
from app.db.seed import METIX_SUSPENSE_ACCOUNT_NUMBER
from app.iso8583.codec import build_0200, build_0210, encode_raw_iso
from app.iso8583.models import Iso8583Message, format_de7
from app.services.ledger import InsufficientFundsError, post_balanced_journal
from app.services.redis_guard import RedisGuard

logger = logging.getLogger(__name__)


class BeneficiaryTransportResult:
    def __init__(
        self,
        *,
        response_message: Iso8583Message | None = None,
        dropped: bool = False,
        timed_out: bool = False,
    ) -> None:
        self.response_message = response_message
        self.dropped = dropped
        self.timed_out = timed_out


class HttpBeneficiaryClient:
    """Sends ISO 8583 0200 / 0420 payloads to the Beneficiary PSP Simulator endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._external_client = http_client

    async def send_iso_message(
        self, msg: Iso8583Message, *, timeout_seconds: float
    ) -> BeneficiaryTransportResult:
        url = f"{self.base_url}/sim/beneficiary/iso8583"
        payload = msg.model_dump(mode="json")

        try:
            if self._external_client is not None:
                resp = await self._external_client.post(url, json=payload, timeout=timeout_seconds)
            else:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(url, json=payload, timeout=timeout_seconds)
        except httpx.TimeoutException:
            logger.warning("Beneficiary HTTP timeout for MTI=%s STAN=%s", msg.mti.value, msg.de11)
            return BeneficiaryTransportResult(timed_out=True)
        except httpx.HTTPError as exc:
            logger.error("Beneficiary HTTP transport error: %s", exc)
            return BeneficiaryTransportResult(dropped=True)

        if resp.status_code == 202:
            # Chaos simulator intentionally dropped 0210 / 0430 response
            return BeneficiaryTransportResult(dropped=True)
        if resp.status_code != 200:
            return BeneficiaryTransportResult(dropped=True)

        body = resp.json()
        if body.get("dropped"):
            return BeneficiaryTransportResult(dropped=True)
        return BeneficiaryTransportResult(
            response_message=Iso8583Message.model_validate(body["iso_message"])
        )


@dataclass
class TransferRequest:
    idempotency_key: str
    originator_account_number: str
    beneficiary_account_number: str
    amount_centavos: int
    stan: str | None = None
    rrn: str | None = None


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
    """METIX / SIMOrede Central Switch Router Engine."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: redis.Redis,
        beneficiary_client: HttpBeneficiaryClient,
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.redis_client = redis_client
        self.redis_guard = RedisGuard(redis_client, settings)
        self.beneficiary_client = beneficiary_client
        self.settings = settings or get_settings()

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

        # Secondary idempotency guard on STAN+RRN when caller supplies custom STAN/RRN
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

        # 2. Validate accounts, enforce regulatory daily limit, post Leg 1 Clearing Hold
        limit_reserved = False
        orig_account_id: uuid.UUID | None = None
        tx_id: uuid.UUID | None = None
        iso_0200: Iso8583Message | None = None

        try:
            async with self.session_factory() as session:
                acct_rows = (
                    await session.execute(
                        select(Account, Institution)
                        .join(Institution, Account.institution_id == Institution.id)
                        .where(
                            Account.account_number.in_(
                                [
                                    req.originator_account_number,
                                    req.beneficiary_account_number,
                                    METIX_SUSPENSE_ACCOUNT_NUMBER,
                                ]
                            )
                        )
                    )
                ).all()
                by_number: dict[str, tuple[Account, Institution]] = {
                    acct.account_number: (acct, inst) for acct, inst in acct_rows
                }

                if req.originator_account_number not in by_number:
                    raise ValueError(f"Unknown originator account: {req.originator_account_number}")
                if req.beneficiary_account_number not in by_number:
                    raise ValueError(
                        f"Unknown beneficiary account: {req.beneficiary_account_number}"
                    )
                if METIX_SUSPENSE_ACCOUNT_NUMBER not in by_number:
                    raise RuntimeError("METIX Suspense clearing account is not seeded")

                orig_acct, orig_inst = by_number[req.originator_account_number]
                ben_acct, ben_inst = by_number[req.beneficiary_account_number]
                suspense_acct, _ = by_number[METIX_SUSPENSE_ACCOUNT_NUMBER]
                orig_account_id = orig_acct.id

                iso_0200 = build_0200(
                    originator_account=orig_acct.account_number,
                    originator_code=orig_inst.code,
                    beneficiary_account=ben_acct.account_number,
                    beneficiary_code=ben_inst.code,
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
                    originator_code=orig_inst.code,
                    beneficiary_code=ben_inst.code,
                    originator_account_id=orig_acct.id,
                    beneficiary_account_id=ben_acct.id,
                    amount_centavos=req.amount_centavos,
                    currency=self.settings.currency_code,
                    status=TransactionStatus.INITIATED.value,
                    sla_deadline_at=sla_deadline,
                )
                session.add(tx)
                await session.flush()
                tx_id = tx.id

                # Log inbound 0200 from Originator PSP
                _record_iso_log(
                    session,
                    transaction_id=tx.id,
                    msg=iso_0200,
                    direction=MessageDirection.INBOUND,
                )

                # 3. Notice No. 1/GBM/2026 atomic daily limit check in Redis
                allowed, used, cap = await self.redis_guard.reserve_daily_limit(
                    orig_acct.id, orig_acct.account_tier, req.amount_centavos
                )
                if not allowed:
                    validate_transition(TransactionStatus(tx.status), TransactionStatus.DECLINED)
                    tx.status = TransactionStatus.DECLINED.value
                    tx.response_code = "61"
                    tx.failure_reason = (
                        f"Exceeds Notice No. 1/GBM/2026 {orig_acct.account_tier} daily limit "
                        f"({cap / 100:,.2f} MT; used {used / 100:,.2f} MT)"
                    )
                    iso_0210_declined = build_0210(iso_0200, response_code="61")
                    _record_iso_log(
                        session,
                        transaction_id=tx.id,
                        msg=iso_0210_declined,
                        direction=MessageDirection.OUTBOUND,
                    )
                    await session.commit()
                    await self.redis_guard.bind_transaction_id(req.idempotency_key, tx.id)
                    full = await load_transaction_full(session, tx.id)
                    return TransferResult(transaction=full, idempotent_replay=False)

                limit_reserved = True

                # 4. Double-entry Leg 1: DEBIT Originator -> CREDIT METIX_SUSPENSE
                try:
                    await post_balanced_journal(
                        session,
                        transaction_id=tx.id,
                        entry_type=JournalEntryType.CLEARING_HOLD,
                        description=(
                            f"Clearing hold for STAN {stan} ({orig_inst.code} -> {ben_inst.code})"
                        ),
                        debit_account_id=orig_acct.id,
                        credit_account_id=suspense_acct.id,
                        amount_centavos=req.amount_centavos,
                    )
                except InsufficientFundsError as exc:
                    await self.redis_guard.release_daily_limit(orig_acct.id, req.amount_centavos)
                    limit_reserved = False
                    validate_transition(TransactionStatus(tx.status), TransactionStatus.DECLINED)
                    tx.status = TransactionStatus.DECLINED.value
                    tx.response_code = "51"
                    tx.failure_reason = str(exc)
                    iso_0210_nsf = build_0210(iso_0200, response_code="51")
                    _record_iso_log(
                        session,
                        transaction_id=tx.id,
                        msg=iso_0210_nsf,
                        direction=MessageDirection.OUTBOUND,
                    )
                    await session.commit()
                    await self.redis_guard.bind_transaction_id(req.idempotency_key, tx.id)
                    full = await load_transaction_full(session, tx.id)
                    return TransferResult(transaction=full, idempotent_replay=False)

                # Transition INITIATED -> ROUTED and log outbound 0200 to Beneficiary PSP
                validate_transition(TransactionStatus(tx.status), TransactionStatus.ROUTED)
                tx.status = TransactionStatus.ROUTED.value
                _record_iso_log(
                    session,
                    transaction_id=tx.id,
                    msg=iso_0200,
                    direction=MessageDirection.OUTBOUND,
                )
                await session.commit()
                await self.redis_guard.bind_transaction_id(req.idempotency_key, tx.id)

        except Exception:
            if tx_id is None:
                await self.redis_guard.release_idempotency_lock(req.idempotency_key)
                if limit_reserved and orig_account_id is not None:
                    await self.redis_guard.release_daily_limit(orig_account_id, req.amount_centavos)
            raise

        assert tx_id is not None and iso_0200 is not None

        # 5. Dispatch 0200 to Beneficiary PSP Simulator within SPIM SLA window
        transport_result = await self.beneficiary_client.send_iso_message(
            iso_0200, timeout_seconds=float(self.settings.spim_sla_seconds)
        )

        if (
            transport_result.dropped
            or transport_result.timed_out
            or transport_result.response_message is None
        ):
            # Transaction remains ROUTED; the background SLA sweeper will handle 0420 reversal
            async with self.session_factory() as session:
                full = await load_transaction_full(session, tx_id)
                return TransferResult(transaction=full, idempotent_replay=False)

        # 6. Process 0210 response under row-level lock to avoid racing the SLA sweeper
        resp_0210 = transport_result.response_message
        async with self.session_factory() as session:
            locked_tx = (
                await session.execute(
                    select(SwitchTransaction).where(SwitchTransaction.id == tx_id).with_for_update()
                )
            ).scalar_one()

            # Always record inbound 0210 for complete wire audit trail
            _record_iso_log(
                session,
                transaction_id=locked_tx.id,
                msg=resp_0210,
                direction=MessageDirection.INBOUND,
            )

            # Strict state machine guard: ignore late 0210 if SLA sweeper already moved past ROUTED
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

            if resp_0210.de39 == "00":
                # Leg 2: DEBIT METIX_SUSPENSE -> CREDIT Beneficiary Account
                await post_balanced_journal(
                    session,
                    transaction_id=locked_tx.id,
                    entry_type=JournalEntryType.SETTLEMENT,
                    description=(
                        f"Final settlement for STAN {locked_tx.stan} to "
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
            else:
                # Beneficiary declined (e.g. DE 39 = 91): reverse clearing hold to Originator
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
                await self.redis_guard.release_daily_limit(
                    locked_tx.originator_account_id, locked_tx.amount_centavos
                )
                validate_transition(TransactionStatus(locked_tx.status), TransactionStatus.DECLINED)
                locked_tx.status = TransactionStatus.DECLINED.value
                locked_tx.response_code = resp_0210.de39
                locked_tx.failure_reason = (
                    f"Beneficiary rejected request (DE 39 = {resp_0210.de39})"
                )

            # Log outbound 0210 delivered back to Originator PSP
            _record_iso_log(
                session,
                transaction_id=locked_tx.id,
                msg=resp_0210,
                direction=MessageDirection.OUTBOUND,
            )
            await session.commit()
            full = await load_transaction_full(session, locked_tx.id)
            return TransferResult(transaction=full, idempotent_replay=False)
