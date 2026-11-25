from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import selectinload

from app.api.schemas import (
    AccountOut,
    IsoMessageOut,
    JournalEntryOut,
    JournalPostingOut,
    LedgerSummaryOut,
    TransactionOut,
    TransferCreateRequest,
    TransferResponse,
)
from app.db.models import (
    Account,
    Institution,
    JournalEntry,
    JournalPosting,
    PostingDirection,
    SwitchTransaction,
)
from app.db.seed import METIX_SUSPENSE_ACCOUNT_NUMBER, seed_database
from app.services.redis_guard import RedisGuard
from app.services.switch_engine import SwitchEngine, TransferRequest, load_transaction_full
from app.workers.sla_sweeper import SlaSweeper
from shared.iso8583.models import ISO_RESPONSE_DESCRIPTIONS
from shared.security import Principal, Role, get_optional_principal

router = APIRouter(tags=["metix-switch"])


def _serialize_journal_entry(
    entry: JournalEntry, account_lookup: dict[uuid.UUID, tuple[str, str]]
) -> JournalEntryOut:
    postings_out = [
        JournalPostingOut(
            id=p.id,
            account_id=p.account_id,
            account_number=account_lookup.get(p.account_id, (None, None))[0],
            institution_code=account_lookup.get(p.account_id, (None, None))[1],
            direction=p.direction,
            amount_centavos=p.amount_centavos,
            created_at=p.created_at,
        )
        for p in entry.postings
    ]
    return JournalEntryOut(
        id=entry.id,
        transaction_id=entry.transaction_id,
        entry_type=entry.entry_type,
        description=entry.description,
        created_at=entry.created_at,
        postings=postings_out,
    )


def _serialize_transaction(
    tx: SwitchTransaction, account_lookup: dict[uuid.UUID, tuple[str, str]]
) -> TransactionOut:
    return TransactionOut(
        id=tx.id,
        idempotency_key=tx.idempotency_key,
        stan=tx.stan,
        rrn=tx.rrn,
        transmission_datetime=tx.transmission_datetime,
        originator_code=tx.originator_code,
        beneficiary_code=tx.beneficiary_code,
        originator_account_id=tx.originator_account_id,
        beneficiary_account_id=tx.beneficiary_account_id,
        originator_account_number=tx.originator_account_number,
        beneficiary_account_number=tx.beneficiary_account_number,
        originator_hold_ref=tx.originator_hold_ref,
        amount_centavos=tx.amount_centavos,
        currency=tx.currency,
        status=tx.status,
        response_code=tx.response_code,
        response_description=(
            ISO_RESPONSE_DESCRIPTIONS.get(tx.response_code) if tx.response_code else None
        ),
        auth_id=tx.auth_id,
        failure_reason=tx.failure_reason,
        fraud_score=tx.fraud_score,
        fraud_status=tx.fraud_status,
        processing_latency_ms=tx.processing_latency_ms,
        signature_verified=tx.signature_verified,
        settlement_cycle_id=tx.settlement_cycle_id,
        sla_deadline_at=tx.sla_deadline_at,
        created_at=tx.created_at,
        updated_at=tx.updated_at,
        iso_messages=[
            IsoMessageOut(
                id=m.id,
                mti=m.mti,
                direction=m.direction,
                stan=m.stan,
                rrn=m.rrn,
                response_code=m.response_code,
                payload_json=m.payload_json,
                raw_iso_snippet=m.raw_iso_snippet,
                signature_key_id=m.signature_key_id,
                signature_verified=m.signature_verified,
                created_at=m.created_at,
            )
            for m in tx.iso_messages
        ],
        journal_entries=[_serialize_journal_entry(e, account_lookup) for e in tx.journal_entries],
    )


async def _load_account_lookup(request: Request) -> dict[uuid.UUID, tuple[str, str]]:
    async with request.app.state.session_factory() as session:
        rows = (
            await session.execute(
                select(Account.id, Account.account_number, Institution.code).join(
                    Institution, Account.institution_id == Institution.id
                )
            )
        ).all()
        return {row[0]: (row[1], row[2]) for row in rows}


@router.get("/health")
async def health_check(request: Request) -> dict[str, str]:
    async with request.app.state.session_factory() as session:
        await session.execute(text("SELECT 1"))
    pong = await request.app.state.redis.ping()
    return {
        "status": "ok",
        "postgres": "connected",
        "redis": "connected" if pong else "error",
    }


@router.post("/api/v1/seed")
async def trigger_seed(request: Request) -> dict[str, int]:
    async with request.app.state.session_factory() as session:
        return await seed_database(session)


@router.post("/api/v1/transfers", response_model=TransferResponse)
async def create_transfer(
    payload: TransferCreateRequest,
    request: Request,
    principal: Principal | None = Depends(get_optional_principal),
) -> TransferResponse:
    settings = request.app.state.settings
    if settings.auth_enforce_transfers:
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="OAuth 2.0 Bearer token required to initiate interbank transfers",
            )
        if principal.role not in (Role.MAKER, Role.ADMIN, Role.SERVICE_ADAPTER):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"RBAC denied: role '{principal.role.value}' cannot initiate transfers",
            )

    engine: SwitchEngine = request.app.state.switch_engine
    try:
        result = await engine.initiate_transfer(
            TransferRequest(
                idempotency_key=payload.idempotency_key,
                originator_account_number=payload.originator_account_number,
                beneficiary_account_number=payload.beneficiary_account_number,
                amount_centavos=payload.amount_centavos,
                stan=payload.stan,
                rrn=payload.rrn,
                actor_id=principal.subject if principal else "anonymous",
                actor_role=principal.role.value if principal else "SYSTEM",
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    account_lookup = await _load_account_lookup(request)
    return TransferResponse(
        idempotent_replay=result.idempotent_replay,
        transaction=_serialize_transaction(result.transaction, account_lookup),
    )


@router.get("/api/v1/transactions", response_model=list[TransactionOut])
async def list_transactions(
    request: Request,
    limit: int = Query(default=40, ge=1, le=200),
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[TransactionOut]:
    account_lookup = await _load_account_lookup(request)
    async with request.app.state.session_factory() as session:
        stmt = (
            select(SwitchTransaction)
            .options(
                selectinload(SwitchTransaction.iso_messages),
                selectinload(SwitchTransaction.journal_entries).selectinload(JournalEntry.postings),
            )
            .order_by(SwitchTransaction.created_at.desc())
            .limit(limit)
        )
        if status_filter:
            stmt = stmt.where(SwitchTransaction.status == status_filter)
        txs = list((await session.execute(stmt)).scalars().all())
        return [_serialize_transaction(tx, account_lookup) for tx in txs]


@router.get("/api/v1/transactions/{transaction_id}", response_model=TransactionOut)
async def get_transaction(transaction_id: uuid.UUID, request: Request) -> TransactionOut:
    account_lookup = await _load_account_lookup(request)
    async with request.app.state.session_factory() as session:
        try:
            tx = await load_transaction_full(session, transaction_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Transaction not found") from exc
        return _serialize_transaction(tx, account_lookup)


@router.get("/api/v1/accounts", response_model=list[AccountOut])
async def list_accounts(request: Request) -> list[AccountOut]:
    """Return METIX Switch clearing suspense and participant settlement accounts."""
    guard = RedisGuard(request.app.state.redis, request.app.state.settings)
    async with request.app.state.session_factory() as session:
        rows = (
            await session.execute(
                select(Account, Institution)
                .join(Institution, Account.institution_id == Institution.id)
                .order_by(
                    Institution.role.asc(), Institution.code.asc(), Account.account_number.asc()
                )
            )
        ).all()

    results: list[AccountOut] = []
    for acct, inst in rows:
        results.append(
            AccountOut(
                id=acct.id,
                institution_code=inst.code,
                institution_name=inst.name,
                institution_role=inst.role,
                bic_code=inst.bic_code,
                account_number=acct.account_number,
                holder_name=acct.holder_name,
                account_tier=acct.account_tier,
                currency=acct.currency,
                balance_centavos=acct.balance_centavos,
                version=acct.version,
                daily_limit_centavos=guard.daily_limit_for_tier(acct.account_tier),
                daily_used_centavos=0,
                updated_at=acct.updated_at,
            )
        )
    return results


@router.get("/api/v1/ledger", response_model=LedgerSummaryOut)
async def get_ledger(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> LedgerSummaryOut:
    account_lookup = await _load_account_lookup(request)
    async with request.app.state.session_factory() as session:
        debits = (
            await session.execute(
                select(func.coalesce(func.sum(JournalPosting.amount_centavos), 0)).where(
                    JournalPosting.direction == PostingDirection.DEBIT.value
                )
            )
        ).scalar_one()
        credits = (
            await session.execute(
                select(func.coalesce(func.sum(JournalPosting.amount_centavos), 0)).where(
                    JournalPosting.direction == PostingDirection.CREDIT.value
                )
            )
        ).scalar_one()
        suspense_balance = (
            await session.execute(
                select(Account.balance_centavos).where(
                    Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER
                )
            )
        ).scalar_one_or_none() or 0

        entries = list(
            (
                await session.execute(
                    select(JournalEntry)
                    .options(selectinload(JournalEntry.postings))
                    .order_by(JournalEntry.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    total_debits = int(debits)
    total_credits = int(credits)
    return LedgerSummaryOut(
        total_debits_centavos=total_debits,
        total_credits_centavos=total_credits,
        is_balanced=total_debits == total_credits,
        suspense_balance_centavos=int(suspense_balance),
        entries=[_serialize_journal_entry(e, account_lookup) for e in entries],
    )


@router.post("/api/v1/sweeper/run")
async def trigger_sweeper_once(request: Request) -> dict[str, list[str]]:
    sweeper: SlaSweeper = request.app.state.sla_sweeper
    processed = await sweeper.sweep_once()
    return {"processed_transaction_ids": [str(tx_id) for tx_id in processed]}
