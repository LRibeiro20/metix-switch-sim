from __future__ import annotations

import asyncio
import json
import secrets
import string
import time
import uuid
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import redis.asyncio as redis
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from cbs.config import CbsSettings
from cbs.db.models import (
    CbsAccount,
    CbsAccountTier,
    CbsHold,
    CbsHoldStatus,
    CbsJournalEntry,
    CbsJournalPosting,
    CbsTransaction,
    CbsTxStatus,
    CbsTxType,
)
from cbs.db.seed import BANK_CLEARING_GL_ACCOUNT
from shared.iso8583.codec import build_0210, build_0430
from shared.iso8583.models import MTI, Iso8583Message
from shared.schemas import (
    BankChaosConfig,
    CbsAccountOut,
    CbsAccountStatus,
    CbsDebitAuthorizeRequest,
    CbsDebitAuthorizeResponse,
    CbsDebitCaptureRequest,
    CbsDebitReleaseRequest,
    CbsHoldActionResponse,
    CbsInternalTransferRequest,
    CbsInternalTransferResponse,
    CbsRestrictionUpdateRequest,
    CbsTransactionOut,
    ReconFaultInjectRequest,
)
from shared.security import FieldEncryptor

_RESERVE_DAILY_LUA = """
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

_RELEASE_DAILY_LUA = """
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


def _gen_auth_id() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


class CbsCoreEngine:
    """Bank-isolated Core Banking Simulator engine with available vs ledger balances."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: redis.Redis,
        settings: CbsSettings,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis_client
        self.settings = settings
        self.encryptor = FieldEncryptor(settings.encryption_key)
        self._reserve_lua = redis_client.register_script(_RESERVE_DAILY_LUA)
        self._release_lua = redis_client.register_script(_RELEASE_DAILY_LUA)

    def _today_cat(self) -> str:
        return datetime.now(ZoneInfo(self.settings.timezone_name)).strftime("%Y-%m-%d")

    def _daily_key(self, account_number: str) -> str:
        return f"cbs:{self.settings.bank_code}:daily:{account_number}:{self._today_cat()}"

    def _chaos_key(self) -> str:
        return f"cbs:{self.settings.bank_code}:chaos"

    async def get_chaos(self) -> BankChaosConfig:
        raw = await self.redis.get(self._chaos_key())
        if raw is None:
            return BankChaosConfig()
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        return BankChaosConfig.model_validate(data)

    async def set_chaos(self, cfg: BankChaosConfig) -> BankChaosConfig:
        await self.redis.set(self._chaos_key(), cfg.model_dump_json())
        return cfg

    async def _daily_used(self, account_number: str) -> int:
        raw = await self.redis.get(self._daily_key(account_number))
        if raw is None:
            return 0
        return int(raw.decode() if isinstance(raw, bytes) else raw)

    def _serialize_account(self, acct: CbsAccount, daily_used: int) -> CbsAccountOut:
        holder_plain = self.encryptor.decrypt(acct.holder_name_enc)
        return CbsAccountOut(
            id=acct.id,
            bank_code=self.settings.bank_code,
            bank_name=self.settings.bank_name,
            bic_code=self.settings.bic_code,
            account_number=acct.account_number,
            holder_name=holder_plain,
            holder_name_masked=acct.holder_name_masked,
            account_tier=acct.account_tier,
            currency=acct.currency,
            ledger_balance_centavos=acct.ledger_balance_centavos,
            available_balance_centavos=acct.available_balance_centavos,
            earmarked_centavos=max(
                0, acct.ledger_balance_centavos - acct.available_balance_centavos
            ),
            status=acct.status,
            per_tx_limit_centavos=acct.per_tx_limit_centavos,
            daily_limit_centavos=acct.daily_limit_centavos,
            daily_used_centavos=daily_used,
            version=acct.version,
            pii_encrypted_at_rest=acct.holder_name_enc.startswith("enc:v1:"),
            updated_at=acct.updated_at,
        )

    async def list_accounts(self, *, include_gl: bool = False) -> list[CbsAccountOut]:
        async with self.session_factory() as session:
            stmt = select(CbsAccount).order_by(CbsAccount.account_number.asc())
            if not include_gl:
                stmt = stmt.where(CbsAccount.account_tier != CbsAccountTier.CLEARING_GL.value)
            accts = list((await session.execute(stmt)).scalars().all())

        out: list[CbsAccountOut] = []
        for a in accts:
            used = await self._daily_used(a.account_number)
            out.append(self._serialize_account(a, used))
        return out

    async def get_account_balance(self, account_number: str) -> CbsAccountOut | None:
        async with self.session_factory() as session:
            acct = (
                await session.execute(
                    select(CbsAccount).where(CbsAccount.account_number == account_number)
                )
            ).scalar_one_or_none()
            if acct is None:
                return None
        used = await self._daily_used(acct.account_number)
        return self._serialize_account(acct, used)

    async def update_restrictions(
        self, account_number: str, req: CbsRestrictionUpdateRequest
    ) -> CbsAccountOut:
        async with self.session_factory() as session:
            acct = (
                await session.execute(
                    select(CbsAccount).where(CbsAccount.account_number == account_number)
                )
            ).scalar_one_or_none()
            if acct is None:
                raise KeyError(f"Account {account_number} not found in {self.settings.bank_code}")

            values: dict[str, object] = {
                "version": CbsAccount.version + 1,
                "updated_at": func.now(),
            }
            if req.status is not None:
                values["status"] = req.status.value
            if req.per_tx_limit_centavos is not None:
                values["per_tx_limit_centavos"] = req.per_tx_limit_centavos
            if req.daily_limit_centavos is not None:
                values["daily_limit_centavos"] = req.daily_limit_centavos

            await session.execute(
                update(CbsAccount).where(CbsAccount.id == acct.id).values(**values)
            )
            await session.commit()
            refreshed = (
                await session.execute(
                    select(CbsAccount)
                    .where(CbsAccount.id == acct.id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
        used = await self._daily_used(refreshed.account_number)
        return self._serialize_account(refreshed, used)

    async def authorize_debit(
        self, req: CbsDebitAuthorizeRequest, *, signature_verified: bool = True
    ) -> CbsDebitAuthorizeResponse:
        """Earmark funds by decrementing available_balance_centavos (ledger_balance unchanged)."""
        chaos = await self.get_chaos()
        if chaos.offline:
            return CbsDebitAuthorizeResponse(
                approved=False,
                response_code="91",
                reason=f"{self.settings.bank_code} CBS simulator is in offline maintenance mode",
            )

        # Idempotent replay check
        async with self.session_factory() as session:
            existing_hold = (
                await session.execute(
                    select(CbsHold).where(CbsHold.idempotency_key == req.idempotency_key)
                )
            ).scalar_one_or_none()
            if existing_hold is not None:
                acct = (
                    await session.execute(
                        select(CbsAccount).where(CbsAccount.id == existing_hold.account_id)
                    )
                ).scalar_one()
                return CbsDebitAuthorizeResponse(
                    approved=True,
                    response_code="00",
                    hold_ref=existing_hold.hold_ref,
                    cbs_ref=f"{self.settings.bank_code}-{existing_hold.stan}",
                    available_balance_centavos=acct.available_balance_centavos,
                    ledger_balance_centavos=acct.ledger_balance_centavos,
                )

        for _attempt in range(6):
            async with self.session_factory() as session:
                acct = (
                    await session.execute(
                        select(CbsAccount)
                        .where(CbsAccount.account_number == req.account_number)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if acct is None:
                    return CbsDebitAuthorizeResponse(
                        approved=False,
                        response_code="14",
                        reason=(
                            f"Account {req.account_number} not found at {self.settings.bank_code}"
                        ),
                    )

                if acct.status in (
                    CbsAccountStatus.FROZEN.value,
                    CbsAccountStatus.CLOSED.value,
                    CbsAccountStatus.PND.value,
                ):
                    return CbsDebitAuthorizeResponse(
                        approved=False,
                        response_code="62",
                        reason=(
                            f"Account {acct.account_number} restricted ({acct.status}) "
                            f"at {self.settings.bank_code}"
                        ),
                        available_balance_centavos=acct.available_balance_centavos,
                        ledger_balance_centavos=acct.ledger_balance_centavos,
                    )

                if req.amount_centavos > acct.per_tx_limit_centavos:
                    return CbsDebitAuthorizeResponse(
                        approved=False,
                        response_code="61",
                        reason=(
                            f"Exceeds {self.settings.bank_code} per-transaction limit "
                            f"({acct.per_tx_limit_centavos / 100:,.2f} MT)"
                        ),
                        available_balance_centavos=acct.available_balance_centavos,
                        ledger_balance_centavos=acct.ledger_balance_centavos,
                    )

                if acct.available_balance_centavos < req.amount_centavos:
                    return CbsDebitAuthorizeResponse(
                        approved=False,
                        response_code="51",
                        reason=(
                            f"Insufficient available balance on {acct.account_number}: "
                            f"{acct.available_balance_centavos} < {req.amount_centavos} centavos"
                        ),
                        available_balance_centavos=acct.available_balance_centavos,
                        ledger_balance_centavos=acct.ledger_balance_centavos,
                    )

                # Reserve CBS daily limit atomically
                daily_res = await self._reserve_lua(
                    keys=[self._daily_key(acct.account_number)],
                    args=[req.amount_centavos, acct.daily_limit_centavos, 172_800],
                )
                if int(daily_res[0]) != 1:
                    return CbsDebitAuthorizeResponse(
                        approved=False,
                        response_code="61",
                        reason=(
                            f"Exceeds {self.settings.bank_code} CBS daily account limit "
                            f"({acct.daily_limit_centavos / 100:,.2f} MT)"
                        ),
                        available_balance_centavos=acct.available_balance_centavos,
                        ledger_balance_centavos=acct.ledger_balance_centavos,
                    )

                # Optimistic lock CAS on available_balance_centavos ONLY (ledger stays unchanged)
                res = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(CbsAccount)
                        .where(
                            CbsAccount.id == acct.id,
                            CbsAccount.version == acct.version,
                            CbsAccount.available_balance_centavos >= req.amount_centavos,
                        )
                        .values(
                            available_balance_centavos=(
                                CbsAccount.available_balance_centavos - req.amount_centavos
                            ),
                            version=CbsAccount.version + 1,
                            updated_at=func.now(),
                        )
                    ),
                )
                if res.rowcount != 1:
                    await self._release_lua(
                        keys=[self._daily_key(acct.account_number)], args=[req.amount_centavos]
                    )
                    await session.rollback()
                    continue

                hold_ref = f"HLD-{self.settings.bank_code}-{uuid.uuid4().hex[:10].upper()}"
                cbs_ref = f"CBS-{self.settings.bank_code}-{uuid.uuid4().hex[:10].upper()}"
                hold = CbsHold(
                    hold_ref=hold_ref,
                    switch_tx_id=req.switch_tx_id,
                    idempotency_key=req.idempotency_key,
                    stan=req.stan,
                    rrn=req.rrn,
                    account_id=acct.id,
                    amount_centavos=req.amount_centavos,
                    status=CbsHoldStatus.ACTIVE.value,
                )
                tx = CbsTransaction(
                    cbs_ref=cbs_ref,
                    switch_tx_id=req.switch_tx_id,
                    idempotency_key=req.idempotency_key,
                    stan=req.stan,
                    rrn=req.rrn,
                    tx_type=CbsTxType.OUTBOUND_DEBIT.value,
                    account_id=acct.id,
                    counterparty_bank=req.counterparty_bank,
                    counterparty_account=req.counterparty_account,
                    amount_centavos=req.amount_centavos,
                    currency="943",
                    status=CbsTxStatus.HELD.value,
                    response_code="00",
                    signature_verified=signature_verified,
                )
                session.add_all([hold, tx])
                await session.commit()

                new_avail = acct.available_balance_centavos - req.amount_centavos
                return CbsDebitAuthorizeResponse(
                    approved=True,
                    response_code="00",
                    hold_ref=hold_ref,
                    cbs_ref=cbs_ref,
                    available_balance_centavos=new_avail,
                    ledger_balance_centavos=acct.ledger_balance_centavos,
                )

        return CbsDebitAuthorizeResponse(
            approved=False,
            response_code="96",
            reason="Optimistic lock contention exhausted during CBS debit authorization",
        )

    async def capture_debit(self, req: CbsDebitCaptureRequest) -> CbsHoldActionResponse:
        """Finalize earmarked hold: reduce ledger_balance_centavos and post internal CBS GL."""
        gl_number = BANK_CLEARING_GL_ACCOUNT[self.settings.bank_code]
        for _attempt in range(6):
            async with self.session_factory() as session:
                hold = (
                    await session.execute(
                        select(CbsHold).where(CbsHold.hold_ref == req.hold_ref).with_for_update()
                    )
                ).scalar_one_or_none()
                if hold is None:
                    raise KeyError(f"Unknown hold_ref: {req.hold_ref}")

                acct = (
                    await session.execute(
                        select(CbsAccount)
                        .where(CbsAccount.id == hold.account_id)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()

                if hold.status == CbsHoldStatus.CAPTURED.value:
                    return CbsHoldActionResponse(
                        ok=True,
                        hold_ref=hold.hold_ref,
                        status=hold.status,
                        available_balance_centavos=acct.available_balance_centavos,
                        ledger_balance_centavos=acct.ledger_balance_centavos,
                    )
                if hold.status != CbsHoldStatus.ACTIVE.value:
                    raise ValueError(f"Hold {hold.hold_ref} is in status {hold.status}")

                gl_acct = (
                    await session.execute(
                        select(CbsAccount)
                        .where(CbsAccount.account_number == gl_number)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()

                # Deterministic UUID order CAS on ledger_balance_centavos
                updates = sorted(
                    [
                        (acct.id, acct.version, -hold.amount_centavos, 0),
                        (gl_acct.id, gl_acct.version, +hold.amount_centavos, +hold.amount_centavos),
                    ],
                    key=lambda item: item[0],
                )
                conflict = False
                for aid, ver, ledger_delta, avail_delta in updates:
                    res = cast(
                        CursorResult[Any],
                        await session.execute(
                            update(CbsAccount)
                            .where(CbsAccount.id == aid, CbsAccount.version == ver)
                            .values(
                                ledger_balance_centavos=CbsAccount.ledger_balance_centavos
                                + ledger_delta,
                                available_balance_centavos=(
                                    CbsAccount.available_balance_centavos + avail_delta
                                ),
                                version=CbsAccount.version + 1,
                                updated_at=func.now(),
                            )
                        ),
                    )
                    if res.rowcount != 1:
                        conflict = True
                        break
                if conflict:
                    await session.rollback()
                    continue

                hold.status = CbsHoldStatus.CAPTURED.value
                cbs_tx = (
                    await session.execute(
                        select(CbsTransaction).where(
                            CbsTransaction.idempotency_key == hold.idempotency_key
                        )
                    )
                ).scalar_one()
                cbs_tx.status = CbsTxStatus.POSTED.value

                je = CbsJournalEntry(
                    cbs_transaction_id=cbs_tx.id,
                    entry_type="OUTBOUND_CAPTURE",
                    description=f"Captured hold {hold.hold_ref} for STAN {req.stan}",
                )
                session.add(je)
                await session.flush()
                session.add_all(
                    [
                        CbsJournalPosting(
                            journal_entry_id=je.id,
                            account_id=acct.id,
                            direction="DEBIT",
                            amount_centavos=hold.amount_centavos,
                        ),
                        CbsJournalPosting(
                            journal_entry_id=je.id,
                            account_id=gl_acct.id,
                            direction="CREDIT",
                            amount_centavos=hold.amount_centavos,
                        ),
                    ]
                )
                await session.commit()
                return CbsHoldActionResponse(
                    ok=True,
                    hold_ref=hold.hold_ref,
                    status=CbsHoldStatus.CAPTURED.value,
                    available_balance_centavos=acct.available_balance_centavos,
                    ledger_balance_centavos=acct.ledger_balance_centavos - hold.amount_centavos,
                )

        raise RuntimeError("Optimistic lock retries exhausted capturing CBS hold")

    async def release_debit(self, req: CbsDebitReleaseRequest) -> CbsHoldActionResponse:
        """Release earmarked hold and restore available_balance_centavos on decline or 0420."""
        for _attempt in range(6):
            async with self.session_factory() as session:
                hold = (
                    await session.execute(
                        select(CbsHold).where(CbsHold.hold_ref == req.hold_ref).with_for_update()
                    )
                ).scalar_one_or_none()
                if hold is None:
                    raise KeyError(f"Unknown hold_ref: {req.hold_ref}")

                acct = (
                    await session.execute(
                        select(CbsAccount)
                        .where(CbsAccount.id == hold.account_id)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()

                if hold.status == CbsHoldStatus.RELEASED.value:
                    return CbsHoldActionResponse(
                        ok=True,
                        hold_ref=hold.hold_ref,
                        status=hold.status,
                        available_balance_centavos=acct.available_balance_centavos,
                        ledger_balance_centavos=acct.ledger_balance_centavos,
                    )
                if hold.status != CbsHoldStatus.ACTIVE.value:
                    return CbsHoldActionResponse(
                        ok=False,
                        hold_ref=hold.hold_ref,
                        status=hold.status,
                        available_balance_centavos=acct.available_balance_centavos,
                        ledger_balance_centavos=acct.ledger_balance_centavos,
                    )

                res = cast(
                    CursorResult[Any],
                    await session.execute(
                        update(CbsAccount)
                        .where(CbsAccount.id == acct.id, CbsAccount.version == acct.version)
                        .values(
                            available_balance_centavos=(
                                CbsAccount.available_balance_centavos + hold.amount_centavos
                            ),
                            version=CbsAccount.version + 1,
                            updated_at=func.now(),
                        )
                    ),
                )
                if res.rowcount != 1:
                    await session.rollback()
                    continue

                hold.status = CbsHoldStatus.RELEASED.value
                cbs_tx = (
                    await session.execute(
                        select(CbsTransaction).where(
                            CbsTransaction.idempotency_key == hold.idempotency_key
                        )
                    )
                ).scalar_one_or_none()
                if cbs_tx is not None:
                    cbs_tx.status = CbsTxStatus.REVERSED.value
                await session.commit()

                await self._release_lua(
                    keys=[self._daily_key(acct.account_number)], args=[hold.amount_centavos]
                )
                return CbsHoldActionResponse(
                    ok=True,
                    hold_ref=hold.hold_ref,
                    status=CbsHoldStatus.RELEASED.value,
                    available_balance_centavos=acct.available_balance_centavos
                    + hold.amount_centavos,
                    ledger_balance_centavos=acct.ledger_balance_centavos,
                )

        raise RuntimeError("Optimistic lock retries exhausted releasing CBS hold")

    async def _load_existing_on_us_response(
        self, req: CbsInternalTransferRequest
    ) -> CbsInternalTransferResponse | None:
        debit_key = f"onus:{req.idempotency_key}:DEBIT"
        async with self.session_factory() as session:
            tx = (
                await session.execute(
                    select(CbsTransaction)
                    .options(selectinload(CbsTransaction.account))
                    .where(CbsTransaction.idempotency_key == debit_key)
                )
            ).scalar_one_or_none()
            if tx is None:
                return None
            ben = (
                await session.execute(
                    select(CbsAccount).where(CbsAccount.account_number == tx.counterparty_account)
                )
            ).scalar_one_or_none()
            return CbsInternalTransferResponse(
                idempotent_replay=True,
                approved=tx.status == CbsTxStatus.POSTED.value,
                bank_code=self.settings.bank_code,
                cbs_ref=tx.cbs_ref,
                auth_id=tx.auth_id,
                response_code=tx.response_code or "00",
                originator_account_number=tx.account.account_number,
                beneficiary_account_number=tx.counterparty_account,
                amount_centavos=tx.amount_centavos,
                currency=tx.currency,
                originator_available_balance_centavos=tx.account.available_balance_centavos,
                originator_ledger_balance_centavos=tx.account.ledger_balance_centavos,
                beneficiary_available_balance_centavos=(
                    ben.available_balance_centavos if ben else None
                ),
                beneficiary_ledger_balance_centavos=ben.ledger_balance_centavos if ben else None,
                processing_latency_ms=0,
            )

    async def execute_internal_transfer(
        self, req: CbsInternalTransferRequest, *, signature_verified: bool = True
    ) -> CbsInternalTransferResponse:
        """Execute an atomic same-bank (on-us) book transfer without METIX/SPIM."""
        started = time.perf_counter()
        if req.amount_centavos <= 0:
            raise ValueError("amount_centavos must be a positive integer")
        if req.originator_account_number == req.beneficiary_account_number:
            raise ValueError("Originator and beneficiary accounts must be distinct")

        idemp_redis_key = f"cbs:{self.settings.bank_code}:onus:idemp:{req.idempotency_key}"
        acquired = await self.redis.set(idemp_redis_key, "PENDING", nx=True, ex=3600)
        if not acquired:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                raw = await self.redis.get(idemp_redis_key)
                if raw is not None and raw != b"PENDING":
                    text = raw.decode() if isinstance(raw, bytes) else str(raw)
                    cached = CbsInternalTransferResponse.model_validate_json(text)
                    return cached.model_copy(update={"idempotent_replay": True})
                existing = await self._load_existing_on_us_response(req)
                if existing is not None:
                    return existing
                await asyncio.sleep(0.03)
            existing = await self._load_existing_on_us_response(req)
            if existing is not None:
                return existing
            raise RuntimeError(
                f"Duplicate on-us transfer for idempotency_key={req.idempotency_key} is still "
                "initializing"
            )

        async def _finalize(resp: CbsInternalTransferResponse) -> CbsInternalTransferResponse:
            await self.redis.set(idemp_redis_key, resp.model_dump_json(), ex=3600)
            return resp

        try:
            existing = await self._load_existing_on_us_response(req)
            if existing is not None:
                return await _finalize(existing)

            chaos = await self.get_chaos()
            if chaos.offline:
                return await _finalize(
                    CbsInternalTransferResponse(
                        idempotent_replay=False,
                        approved=False,
                        bank_code=self.settings.bank_code,
                        response_code="91",
                        reason=(
                            f"{self.settings.bank_code} CBS simulator is in offline "
                            "maintenance mode"
                        ),
                        originator_account_number=req.originator_account_number,
                        beneficiary_account_number=req.beneficiary_account_number,
                        amount_centavos=req.amount_centavos,
                        processing_latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                )

            debit_idemp_key = f"onus:{req.idempotency_key}:DEBIT"
            credit_idemp_key = f"onus:{req.idempotency_key}:CREDIT"

            for _attempt in range(6):
                async with self.session_factory() as session:
                    rows = (
                        await session.execute(
                            select(CbsAccount)
                            .where(
                                CbsAccount.account_number.in_(
                                    [req.originator_account_number, req.beneficiary_account_number]
                                )
                            )
                            .execution_options(populate_existing=True)
                        )
                    ).scalars()
                    by_no = {a.account_number: a for a in rows}
                    orig = by_no.get(req.originator_account_number)
                    ben = by_no.get(req.beneficiary_account_number)

                    if orig is None or ben is None:
                        missing = (
                            req.originator_account_number
                            if orig is None
                            else req.beneficiary_account_number
                        )
                        return await _finalize(
                            CbsInternalTransferResponse(
                                idempotent_replay=False,
                                approved=False,
                                bank_code=self.settings.bank_code,
                                response_code="14",
                                reason=(
                                    f"Account {missing} not found at {self.settings.bank_code}"
                                ),
                                originator_account_number=req.originator_account_number,
                                beneficiary_account_number=req.beneficiary_account_number,
                                amount_centavos=req.amount_centavos,
                                processing_latency_ms=int((time.perf_counter() - started) * 1000),
                            )
                        )

                    if orig.status in (
                        CbsAccountStatus.FROZEN.value,
                        CbsAccountStatus.CLOSED.value,
                        CbsAccountStatus.PND.value,
                    ):
                        return await _finalize(
                            CbsInternalTransferResponse(
                                idempotent_replay=False,
                                approved=False,
                                bank_code=self.settings.bank_code,
                                response_code="62",
                                reason=(
                                    f"Originator account {orig.account_number} restricted "
                                    f"({orig.status}) at {self.settings.bank_code}"
                                ),
                                originator_account_number=orig.account_number,
                                beneficiary_account_number=ben.account_number,
                                amount_centavos=req.amount_centavos,
                                originator_available_balance_centavos=orig.available_balance_centavos,
                                originator_ledger_balance_centavos=orig.ledger_balance_centavos,
                                beneficiary_available_balance_centavos=ben.available_balance_centavos,
                                beneficiary_ledger_balance_centavos=ben.ledger_balance_centavos,
                                processing_latency_ms=int((time.perf_counter() - started) * 1000),
                            )
                        )

                    if ben.status in (
                        CbsAccountStatus.FROZEN.value,
                        CbsAccountStatus.CLOSED.value,
                        CbsAccountStatus.PNC.value,
                    ):
                        return await _finalize(
                            CbsInternalTransferResponse(
                                idempotent_replay=False,
                                approved=False,
                                bank_code=self.settings.bank_code,
                                response_code="62",
                                reason=(
                                    f"Beneficiary account {ben.account_number} restricted "
                                    f"({ben.status}) at {self.settings.bank_code}"
                                ),
                                originator_account_number=orig.account_number,
                                beneficiary_account_number=ben.account_number,
                                amount_centavos=req.amount_centavos,
                                originator_available_balance_centavos=orig.available_balance_centavos,
                                originator_ledger_balance_centavos=orig.ledger_balance_centavos,
                                beneficiary_available_balance_centavos=ben.available_balance_centavos,
                                beneficiary_ledger_balance_centavos=ben.ledger_balance_centavos,
                                processing_latency_ms=int((time.perf_counter() - started) * 1000),
                            )
                        )

                    if req.amount_centavos > orig.per_tx_limit_centavos:
                        return await _finalize(
                            CbsInternalTransferResponse(
                                idempotent_replay=False,
                                approved=False,
                                bank_code=self.settings.bank_code,
                                response_code="61",
                                reason=(
                                    f"Exceeds {self.settings.bank_code} per-transaction limit "
                                    f"({orig.per_tx_limit_centavos / 100:,.2f} MT)"
                                ),
                                originator_account_number=orig.account_number,
                                beneficiary_account_number=ben.account_number,
                                amount_centavos=req.amount_centavos,
                                originator_available_balance_centavos=orig.available_balance_centavos,
                                originator_ledger_balance_centavos=orig.ledger_balance_centavos,
                                beneficiary_available_balance_centavos=ben.available_balance_centavos,
                                beneficiary_ledger_balance_centavos=ben.ledger_balance_centavos,
                                processing_latency_ms=int((time.perf_counter() - started) * 1000),
                            )
                        )

                    if orig.available_balance_centavos < req.amount_centavos:
                        return await _finalize(
                            CbsInternalTransferResponse(
                                idempotent_replay=False,
                                approved=False,
                                bank_code=self.settings.bank_code,
                                response_code="51",
                                reason=(
                                    f"Insufficient available balance on {orig.account_number}: "
                                    f"{orig.available_balance_centavos} < "
                                    f"{req.amount_centavos} centavos"
                                ),
                                originator_account_number=orig.account_number,
                                beneficiary_account_number=ben.account_number,
                                amount_centavos=req.amount_centavos,
                                originator_available_balance_centavos=orig.available_balance_centavos,
                                originator_ledger_balance_centavos=orig.ledger_balance_centavos,
                                beneficiary_available_balance_centavos=ben.available_balance_centavos,
                                beneficiary_ledger_balance_centavos=ben.ledger_balance_centavos,
                                processing_latency_ms=int((time.perf_counter() - started) * 1000),
                            )
                        )

                    daily_res = await self._reserve_lua(
                        keys=[self._daily_key(orig.account_number)],
                        args=[req.amount_centavos, orig.daily_limit_centavos, 172_800],
                    )
                    if int(daily_res[0]) != 1:
                        return await _finalize(
                            CbsInternalTransferResponse(
                                idempotent_replay=False,
                                approved=False,
                                bank_code=self.settings.bank_code,
                                response_code="61",
                                reason=(
                                    f"Exceeds {self.settings.bank_code} CBS daily account limit "
                                    f"({orig.daily_limit_centavos / 100:,.2f} MT)"
                                ),
                                originator_account_number=orig.account_number,
                                beneficiary_account_number=ben.account_number,
                                amount_centavos=req.amount_centavos,
                                originator_available_balance_centavos=orig.available_balance_centavos,
                                originator_ledger_balance_centavos=orig.ledger_balance_centavos,
                                beneficiary_available_balance_centavos=ben.available_balance_centavos,
                                beneficiary_ledger_balance_centavos=ben.ledger_balance_centavos,
                                processing_latency_ms=int((time.perf_counter() - started) * 1000),
                            )
                        )

                    orig_avail_after = orig.available_balance_centavos - req.amount_centavos
                    orig_ledger_after = orig.ledger_balance_centavos - req.amount_centavos
                    ben_avail_after = ben.available_balance_centavos + req.amount_centavos
                    ben_ledger_after = ben.ledger_balance_centavos + req.amount_centavos

                    # Deterministic UUID-ordered CAS directly between customer accounts (no GL)
                    updates = sorted(
                        [
                            (orig.id, orig.version, -req.amount_centavos, True),
                            (ben.id, ben.version, +req.amount_centavos, False),
                        ],
                        key=lambda item: item[0],
                    )
                    conflict = False
                    for aid, ver, delta, check_avail in updates:
                        conds = [CbsAccount.id == aid, CbsAccount.version == ver]
                        if check_avail:
                            conds.append(
                                CbsAccount.available_balance_centavos >= req.amount_centavos
                            )
                        res = cast(
                            CursorResult[Any],
                            await session.execute(
                                update(CbsAccount)
                                .where(*conds)
                                .values(
                                    ledger_balance_centavos=CbsAccount.ledger_balance_centavos
                                    + delta,
                                    available_balance_centavos=(
                                        CbsAccount.available_balance_centavos + delta
                                    ),
                                    version=CbsAccount.version + 1,
                                    updated_at=func.now(),
                                )
                            ),
                        )
                        if res.rowcount != 1:
                            conflict = True
                            break

                    if conflict:
                        await self._release_lua(
                            keys=[self._daily_key(orig.account_number)],
                            args=[req.amount_centavos],
                        )
                        await session.rollback()
                        continue

                    token = uuid.uuid4().hex[:10].upper()
                    cbs_ref = f"ONUS-{self.settings.bank_code}-{token}"
                    auth_id = _gen_auth_id()
                    rrn = f"ONUS{token[:8]}"

                    debit_tx = CbsTransaction(
                        cbs_ref=cbs_ref,
                        switch_tx_id=None,
                        idempotency_key=debit_idemp_key,
                        stan="000000",
                        rrn=rrn,
                        tx_type=CbsTxType.ON_US_DEBIT.value,
                        account_id=orig.id,
                        counterparty_bank=self.settings.bank_code,
                        counterparty_account=ben.account_number,
                        amount_centavos=req.amount_centavos,
                        currency="943",
                        status=CbsTxStatus.POSTED.value,
                        auth_id=auth_id,
                        response_code="00",
                        signature_verified=signature_verified,
                    )
                    credit_tx = CbsTransaction(
                        cbs_ref=f"{cbs_ref}-CR",
                        switch_tx_id=None,
                        idempotency_key=credit_idemp_key,
                        stan="000000",
                        rrn=rrn,
                        tx_type=CbsTxType.ON_US_CREDIT.value,
                        account_id=ben.id,
                        counterparty_bank=self.settings.bank_code,
                        counterparty_account=orig.account_number,
                        amount_centavos=req.amount_centavos,
                        currency="943",
                        status=CbsTxStatus.POSTED.value,
                        auth_id=auth_id,
                        response_code="00",
                        signature_verified=signature_verified,
                    )
                    session.add_all([debit_tx, credit_tx])
                    await session.flush()

                    je = CbsJournalEntry(
                        cbs_transaction_id=debit_tx.id,
                        entry_type="ON_US_TRANSFER",
                        description=(
                            f"Direct on-us book transfer {cbs_ref}: "
                            f"{orig.account_number} -> {ben.account_number}"
                        ),
                    )
                    session.add(je)
                    await session.flush()
                    session.add_all(
                        [
                            CbsJournalPosting(
                                journal_entry_id=je.id,
                                account_id=orig.id,
                                direction="DEBIT",
                                amount_centavos=req.amount_centavos,
                            ),
                            CbsJournalPosting(
                                journal_entry_id=je.id,
                                account_id=ben.id,
                                direction="CREDIT",
                                amount_centavos=req.amount_centavos,
                            ),
                        ]
                    )
                    await session.commit()

                    return await _finalize(
                        CbsInternalTransferResponse(
                            idempotent_replay=False,
                            approved=True,
                            bank_code=self.settings.bank_code,
                            cbs_ref=cbs_ref,
                            auth_id=auth_id,
                            response_code="00",
                            originator_account_number=orig.account_number,
                            beneficiary_account_number=ben.account_number,
                            amount_centavos=req.amount_centavos,
                            currency="943",
                            originator_available_balance_centavos=orig_avail_after,
                            originator_ledger_balance_centavos=orig_ledger_after,
                            beneficiary_available_balance_centavos=ben_avail_after,
                            beneficiary_ledger_balance_centavos=ben_ledger_after,
                            processing_latency_ms=int((time.perf_counter() - started) * 1000),
                        )
                    )

            return await _finalize(
                CbsInternalTransferResponse(
                    idempotent_replay=False,
                    approved=False,
                    bank_code=self.settings.bank_code,
                    response_code="96",
                    reason=("Optimistic lock contention exhausted during CBS on-us book transfer"),
                    originator_account_number=req.originator_account_number,
                    beneficiary_account_number=req.beneficiary_account_number,
                    amount_centavos=req.amount_centavos,
                    processing_latency_ms=int((time.perf_counter() - started) * 1000),
                )
            )
        except Exception:
            await self.redis.delete(idemp_redis_key)
            raise

    async def process_iso8583(
        self,
        msg: Iso8583Message,
        *,
        switch_tx_id: uuid.UUID | None = None,
        signature_verified: bool = True,
    ) -> tuple[int, dict[str, object]]:
        """Process inbound 0200 Beneficiary Credit or 0420 Reversal Advice."""
        chaos = await self.get_chaos()
        if chaos.offline:
            return 503, {"dropped": True, "reason": f"{self.settings.bank_code} CBS offline"}

        if msg.mti == MTI.FINANCIAL_REQUEST:
            if chaos.drop_0210:
                await asyncio.sleep(min(chaos.latency_ms, 250) / 1000.0)
                return 202, {
                    "dropped": True,
                    "beneficiary": self.settings.bank_code,
                    "reason": "Chaos drop_0210 active — 0210 ACK dropped on wire",
                }

            if chaos.latency_ms > 0:
                await asyncio.sleep(chaos.latency_ms / 1000.0)

            if chaos.force_de39 and chaos.force_de39 != "00":
                resp = build_0210(msg, response_code=chaos.force_de39)
                return 200, {"dropped": False, "iso_message": resp.model_dump(mode="json")}

            return await self._credit_beneficiary_0200(
                msg, switch_tx_id=switch_tx_id, signature_verified=signature_verified
            )

        if msg.mti == MTI.REVERSAL_ADVICE:
            if chaos.fail_0430:
                await asyncio.sleep(0.1)
                return 202, {
                    "dropped": True,
                    "beneficiary": self.settings.bank_code,
                    "reason": "Chaos fail_0430 active — 0430 reversal response dropped",
                }
            await asyncio.sleep(0.05)
            return await self._reverse_beneficiary_0420(msg)

        return 400, {"detail": f"Unsupported inbound CBS MTI: {msg.mti.value}"}

    async def _credit_beneficiary_0200(
        self,
        msg: Iso8583Message,
        *,
        switch_tx_id: uuid.UUID | None,
        signature_verified: bool,
    ) -> tuple[int, dict[str, object]]:
        orig_bank, orig_acct = msg.de102.split(":", 1)
        _, ben_acct_no = msg.de103.split(":", 1)
        gl_number = BANK_CLEARING_GL_ACCOUNT[self.settings.bank_code]
        idemp_key = f"credit:{msg.de11}:{msg.de37}"

        for _attempt in range(6):
            async with self.session_factory() as session:
                # Idempotent check
                existing = (
                    await session.execute(
                        select(CbsTransaction).where(CbsTransaction.idempotency_key == idemp_key)
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    resp = build_0210(
                        msg, response_code=existing.response_code or "00", auth_id=existing.auth_id
                    )
                    return 200, {"dropped": False, "iso_message": resp.model_dump(mode="json")}

                acct = (
                    await session.execute(
                        select(CbsAccount)
                        .where(CbsAccount.account_number == ben_acct_no)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if acct is None:
                    resp = build_0210(msg, response_code="14")
                    return 200, {"dropped": False, "iso_message": resp.model_dump(mode="json")}

                if acct.status in (
                    CbsAccountStatus.FROZEN.value,
                    CbsAccountStatus.CLOSED.value,
                    CbsAccountStatus.PNC.value,
                ):
                    resp = build_0210(msg, response_code="62")
                    return 200, {"dropped": False, "iso_message": resp.model_dump(mode="json")}

                gl_acct = (
                    await session.execute(
                        select(CbsAccount)
                        .where(CbsAccount.account_number == gl_number)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()

                updates = sorted(
                    [
                        (gl_acct.id, gl_acct.version, -msg.de4),
                        (acct.id, acct.version, +msg.de4),
                    ],
                    key=lambda item: item[0],
                )
                conflict = False
                for aid, ver, delta in updates:
                    res = cast(
                        CursorResult[Any],
                        await session.execute(
                            update(CbsAccount)
                            .where(CbsAccount.id == aid, CbsAccount.version == ver)
                            .values(
                                ledger_balance_centavos=CbsAccount.ledger_balance_centavos + delta,
                                available_balance_centavos=CbsAccount.available_balance_centavos
                                + delta,
                                version=CbsAccount.version + 1,
                                updated_at=func.now(),
                            )
                        ),
                    )
                    if res.rowcount != 1:
                        conflict = True
                        break
                if conflict:
                    await session.rollback()
                    continue

                auth_id = _gen_auth_id()
                cbs_tx = CbsTransaction(
                    cbs_ref=f"CBS-{self.settings.bank_code}-{uuid.uuid4().hex[:10].upper()}",
                    switch_tx_id=switch_tx_id,
                    idempotency_key=idemp_key,
                    stan=msg.de11,
                    rrn=msg.de37,
                    tx_type=CbsTxType.INBOUND_CREDIT.value,
                    account_id=acct.id,
                    counterparty_bank=orig_bank,
                    counterparty_account=orig_acct,
                    amount_centavos=msg.de4,
                    currency="943",
                    status=CbsTxStatus.POSTED.value,
                    auth_id=auth_id,
                    response_code="00",
                    signature_verified=signature_verified,
                )
                session.add(cbs_tx)
                await session.flush()

                je = CbsJournalEntry(
                    cbs_transaction_id=cbs_tx.id,
                    entry_type="INBOUND_CREDIT",
                    description=f"Inbound interbank credit STAN {msg.de11} from {orig_bank}",
                )
                session.add(je)
                await session.flush()
                session.add_all(
                    [
                        CbsJournalPosting(
                            journal_entry_id=je.id,
                            account_id=gl_acct.id,
                            direction="DEBIT",
                            amount_centavos=msg.de4,
                        ),
                        CbsJournalPosting(
                            journal_entry_id=je.id,
                            account_id=acct.id,
                            direction="CREDIT",
                            amount_centavos=msg.de4,
                        ),
                    ]
                )
                await session.commit()

                resp = build_0210(msg, response_code="00", auth_id=auth_id)
                return 200, {"dropped": False, "iso_message": resp.model_dump(mode="json")}

        resp = build_0210(msg, response_code="96")
        return 200, {"dropped": False, "iso_message": resp.model_dump(mode="json")}

    async def _reverse_beneficiary_0420(self, msg: Iso8583Message) -> tuple[int, dict[str, object]]:
        gl_number = BANK_CLEARING_GL_ACCOUNT[self.settings.bank_code]
        idemp_key = f"credit:{msg.de11}:{msg.de37}"

        async with self.session_factory() as session:
            existing = (
                await session.execute(
                    select(CbsTransaction)
                    .where(CbsTransaction.idempotency_key == idemp_key)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None and existing.status == CbsTxStatus.POSTED.value:
                acct = (
                    await session.execute(
                        select(CbsAccount)
                        .where(CbsAccount.id == existing.account_id)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                gl_acct = (
                    await session.execute(
                        select(CbsAccount)
                        .where(CbsAccount.account_number == gl_number)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if acct.available_balance_centavos >= existing.amount_centavos:
                    await session.execute(
                        update(CbsAccount)
                        .where(CbsAccount.id == acct.id)
                        .values(
                            ledger_balance_centavos=(
                                CbsAccount.ledger_balance_centavos - existing.amount_centavos
                            ),
                            available_balance_centavos=(
                                CbsAccount.available_balance_centavos - existing.amount_centavos
                            ),
                            version=CbsAccount.version + 1,
                        )
                    )
                    await session.execute(
                        update(CbsAccount)
                        .where(CbsAccount.id == gl_acct.id)
                        .values(
                            ledger_balance_centavos=(
                                CbsAccount.ledger_balance_centavos + existing.amount_centavos
                            ),
                            available_balance_centavos=(
                                CbsAccount.available_balance_centavos + existing.amount_centavos
                            ),
                            version=CbsAccount.version + 1,
                        )
                    )
                    existing.status = CbsTxStatus.REVERSED.value
                    await session.commit()

        resp = build_0430(msg, response_code="00")
        return 200, {"dropped": False, "iso_message": resp.model_dump(mode="json")}

    async def list_transactions(self, limit: int = 100) -> list[CbsTransactionOut]:
        async with self.session_factory() as session:
            stmt = (
                select(CbsTransaction)
                .options(selectinload(CbsTransaction.account))
                .order_by(CbsTransaction.created_at.desc())
                .limit(limit)
            )
            txs = list((await session.execute(stmt)).scalars().all())

        return [
            CbsTransactionOut(
                id=t.id,
                cbs_ref=t.cbs_ref,
                switch_tx_id=t.switch_tx_id,
                idempotency_key=t.idempotency_key,
                stan=t.stan,
                rrn=t.rrn,
                tx_type=t.tx_type,
                bank_code=self.settings.bank_code,
                account_number=t.account.account_number,
                counterparty_bank=t.counterparty_bank,
                counterparty_account=t.counterparty_account,
                amount_centavos=t.amount_centavos,
                currency=t.currency,
                status=t.status,
                auth_id=t.auth_id,
                response_code=t.response_code,
                signature_verified=t.signature_verified,
                created_at=t.created_at,
            )
            for t in txs
        ]

    async def inject_recon_fault(self, req: ReconFaultInjectRequest) -> CbsTransactionOut:
        """Controlled reconciliation fault injector for testing 3-way recon exception detection."""
        async with self.session_factory() as session:
            acct = (
                await session.execute(
                    select(CbsAccount).where(CbsAccount.account_number == req.account_number)
                )
            ).scalar_one_or_none()
            if acct is None:
                raise KeyError(f"Unknown CBS account {req.account_number}")

            stan = req.stan or f"{secrets.randbelow(899999) + 100000:06d}"
            rrn = req.rrn or f"632912{stan}"
            tx_type = CbsTxType.INBOUND_CREDIT.value
            status = CbsTxStatus.POSTED.value

            if req.fault_type == "STALE_ACTIVE_HOLD":
                tx_type = CbsTxType.OUTBOUND_DEBIT.value
                status = CbsTxStatus.HELD.value
                hold = CbsHold(
                    hold_ref=f"HLD-FAULT-{uuid.uuid4().hex[:8].upper()}",
                    switch_tx_id=req.switch_tx_id or uuid.uuid4(),
                    idempotency_key=f"fault-hold-{uuid.uuid4().hex}",
                    stan=stan,
                    rrn=rrn,
                    account_id=acct.id,
                    amount_centavos=req.amount_centavos,
                    status=CbsHoldStatus.ACTIVE.value,
                )
                session.add(hold)

            tx = CbsTransaction(
                cbs_ref=f"CBS-FLT-{uuid.uuid4().hex[:8].upper()}",
                switch_tx_id=req.switch_tx_id,
                idempotency_key=f"fault-{req.fault_type.lower()}-{uuid.uuid4().hex[:10]}",
                stan=stan,
                rrn=rrn,
                tx_type=tx_type,
                account_id=acct.id,
                counterparty_bank="STDB_MZ" if self.settings.bank_code == "BCI_MZ" else "BCI_MZ",
                counterparty_account="00030055443322"
                if self.settings.bank_code == "BCI_MZ"
                else "00010012345678",
                amount_centavos=req.amount_centavos,
                currency="943",
                status=status,
                auth_id=_gen_auth_id(),
                response_code="00",
                signature_verified=True,
            )
            session.add(tx)
            await session.commit()
            await session.refresh(tx)

            return CbsTransactionOut(
                id=tx.id,
                cbs_ref=tx.cbs_ref,
                switch_tx_id=tx.switch_tx_id,
                idempotency_key=tx.idempotency_key,
                stan=tx.stan,
                rrn=tx.rrn,
                tx_type=tx.tx_type,
                bank_code=self.settings.bank_code,
                account_number=acct.account_number,
                counterparty_bank=tx.counterparty_bank,
                counterparty_account=tx.counterparty_account,
                amount_centavos=tx.amount_centavos,
                currency=tx.currency,
                status=tx.status,
                auth_id=tx.auth_id,
                response_code=tx.response_code,
                signature_verified=tx.signature_verified,
                created_at=tx.created_at,
            )
