from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis.asyncio as redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Account,
    InvalidStateTransitionError,
    JournalEntryType,
    PostingDirection,
    SwitchTransaction,
    TransactionStatus,
    validate_transition,
)
from app.db.seed import METIX_SUSPENSE_ACCOUNT_NUMBER
from app.iso8583.codec import build_0200, build_0420, decode_raw_iso, encode_raw_iso
from app.services.ledger import post_balanced_journal

BCI_INDIVIDUAL = "00010012345678"  # Amélia Machel (500,000.00 MT balance, 200,000.00 MT daily cap)
STDB_INDIVIDUAL = "00030055443322"  # Carlos Mondlane (400,000.00 MT balance, 200,000.00 MT cap)
MPESA_WALLET = "258841234567"  # Rosa Sitoe (50,000.00 MT balance)
EMOLA_WALLET = "258869876543"  # João Mabunda (35,000.00 MT balance)


def test_iso8583_wire_codec_round_trip() -> None:
    """Verify canonical ISO 8583 0200 and 0420 ASCII wire frames round-trip losslessly."""
    msg_0200 = build_0200(
        originator_account=BCI_INDIVIDUAL,
        originator_code="BCI_MZ",
        beneficiary_account=MPESA_WALLET,
        beneficiary_code="MPESA_MZ",
        amount_centavos=125_050,
        stan="000042",
        rrn="632620000042",
        de7="1122201500",
    )
    raw_0200 = encode_raw_iso(msg_0200)
    assert raw_0200.startswith("0200")
    decoded_0200 = decode_raw_iso(raw_0200)
    assert decoded_0200 == msg_0200

    msg_0420 = build_0420(
        originator_account=BCI_INDIVIDUAL,
        originator_code="BCI_MZ",
        beneficiary_account=MPESA_WALLET,
        beneficiary_code="MPESA_MZ",
        amount_centavos=125_050,
        orig_stan="000042",
        orig_rrn="632620000042",
        orig_de7="1122201500",
        response_code="68",
    )
    raw_0420 = encode_raw_iso(msg_0420)
    assert raw_0420.startswith("0420")
    decoded_0420 = decode_raw_iso(raw_0420)
    assert decoded_0420 == msg_0420
    assert decoded_0420.de90 is not None
    assert len(decoded_0420.de90) == 42


def test_terminal_states_never_transition_backward() -> None:
    """Verify strict state machine rejects illegal backward transitions."""
    for terminal in (
        TransactionStatus.COMPLETED,
        TransactionStatus.DECLINED,
        TransactionStatus.REVERSED,
        TransactionStatus.RECON_HOLD,
    ):
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(terminal, TransactionStatus.ROUTED)
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(terminal, TransactionStatus.COMPLETED)


@pytest.mark.asyncio
async def test_1_happy_path_0200_to_0210_balanced_double_entry(
    clean_env: tuple[
        httpx.AsyncClient,
        async_sessionmaker[AsyncSession],
        redis.Redis,
        object,
    ],
) -> None:
    """Scenario 1: Happy-path 0200 -> 0210 verifying balanced DEBIT/CREDIT postings."""
    client, session_factory, _, _ = clean_env

    # Configure fast normal chaos mode (10ms)
    chaos_resp = await client.post(
        "/sim/beneficiary/chaos",
        json={"preset": "normal", "latency_ms": 10, "drop_0210": False, "force_de39": None},
    )
    assert chaos_resp.status_code == 200

    amount = 750_000  # 7,500.00 MT
    resp = await client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"happy-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": MPESA_WALLET,
            "amount_centavos": amount,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["idempotent_replay"] is False
    tx = body["transaction"]
    assert tx["status"] == TransactionStatus.COMPLETED.value
    assert tx["response_code"] == "00"
    assert tx["currency"] == "943"
    assert tx["auth_id"] is not None and len(tx["auth_id"]) == 6

    # Verify ISO 8583 trace contains inbound/outbound 0200 and inbound/outbound 0210
    mtis = [(m["mti"], m["direction"]) for m in tx["iso_messages"]]
    assert ("0200", "INBOUND") in mtis
    assert ("0200", "OUTBOUND") in mtis
    assert ("0210", "INBOUND") in mtis
    assert ("0210", "OUTBOUND") in mtis
    for m in tx["iso_messages"]:
        assert m["payload_json"]["de49"] == "943"
        assert m["payload_json"]["de4"] == amount
        assert len(m["raw_iso_snippet"]) >= 36

    # Verify 2 balanced journal entries: CLEARING_HOLD + SETTLEMENT
    entries = tx["journal_entries"]
    assert len(entries) == 2
    assert {e["entry_type"] for e in entries} == {
        JournalEntryType.CLEARING_HOLD.value,
        JournalEntryType.SETTLEMENT.value,
    }
    for entry in entries:
        debits = sum(
            p["amount_centavos"]
            for p in entry["postings"]
            if p["direction"] == PostingDirection.DEBIT.value
        )
        credits = sum(
            p["amount_centavos"]
            for p in entry["postings"]
            if p["direction"] == PostingDirection.CREDIT.value
        )
        assert debits == credits == amount

    # Verify final DB balances & incremented optimistic lock versions
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Account).where(
                    Account.account_number.in_(
                        [BCI_INDIVIDUAL, MPESA_WALLET, METIX_SUSPENSE_ACCOUNT_NUMBER]
                    )
                )
            )
        ).scalars()
        accts = {a.account_number: a for a in rows}
        assert accts[BCI_INDIVIDUAL].balance_centavos == 50_000_000 - amount
        assert accts[MPESA_WALLET].balance_centavos == 5_000_000 + amount
        assert accts[METIX_SUSPENSE_ACCOUNT_NUMBER].balance_centavos == 0
        assert accts[BCI_INDIVIDUAL].version == 2
        assert accts[MPESA_WALLET].version == 2
        assert accts[METIX_SUSPENSE_ACCOUNT_NUMBER].version == 3

    ledger = (await client.get("/api/v1/ledger")).json()
    assert ledger["is_balanced"] is True
    assert ledger["total_debits_centavos"] == ledger["total_credits_centavos"] == amount * 2
    assert ledger["suspense_balance_centavos"] == 0


@pytest.mark.asyncio
async def test_2_duplicate_retry_idempotency_single_execution(
    clean_env: tuple[
        httpx.AsyncClient,
        async_sessionmaker[AsyncSession],
        redis.Redis,
        object,
    ],
) -> None:
    """Scenario 2: Concurrent + sequential retries with identical idempotency key execute once."""
    client, session_factory, _, _ = clean_env

    await client.post(
        "/sim/beneficiary/chaos",
        json={"preset": "normal", "latency_ms": 50, "drop_0210": False, "force_de39": None},
    )

    idemp_key = f"storm-{uuid.uuid4().hex}"
    amount = 500_000  # 5,000.00 MT
    payload = {
        "idempotency_key": idemp_key,
        "originator_account_number": BCI_INDIVIDUAL,
        "beneficiary_account_number": MPESA_WALLET,
        "amount_centavos": amount,
    }

    # Fire 5 concurrent duplicate requests simultaneously (retry storm)
    responses = await asyncio.gather(
        *(client.post("/api/v1/transfers", json=payload) for _ in range(5))
    )
    for r in responses:
        assert r.status_code == 200

    # Plus one sequential duplicate retry after completion
    seq_resp = await client.post("/api/v1/transfers", json=payload)
    assert seq_resp.status_code == 200
    assert seq_resp.json()["idempotent_replay"] is True

    tx_ids = {r.json()["transaction"]["id"] for r in responses}
    tx_ids.add(seq_resp.json()["transaction"]["id"])
    assert len(tx_ids) == 1

    # Exactly 1 request executed freshly, all others were idempotent replays
    fresh_count = sum(1 for r in responses if r.json()["idempotent_replay"] is False)
    assert fresh_count == 1

    # Originator must only be debited ONCE
    async with session_factory() as session:
        orig = (
            await session.execute(select(Account).where(Account.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        ben = (
            await session.execute(select(Account).where(Account.account_number == MPESA_WALLET))
        ).scalar_one()
        assert orig.balance_centavos == 50_000_000 - amount
        assert ben.balance_centavos == 5_000_000 + amount


@pytest.mark.asyncio
async def test_3_beneficiary_timeout_over_25s_sla_triggers_0420_reversal(
    clean_env: tuple[
        httpx.AsyncClient,
        async_sessionmaker[AsyncSession],
        redis.Redis,
        object,
    ],
) -> None:
    """Scenario 3: Dropped 0210 (>25s SLA) triggers automatic 0420 reversal & balance restore."""
    client, session_factory, _, sweeper = clean_env

    # Enable Drop 0210 ACK chaos mode
    await client.post("/sim/beneficiary/chaos", json={"preset": "drop_0210"})

    amount = 1_200_000  # 12,000.00 MT
    resp = await client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"timeout-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": MPESA_WALLET,
            "amount_centavos": amount,
        },
    )
    assert resp.status_code == 200
    tx = resp.json()["transaction"]
    tx_id = uuid.UUID(tx["id"])
    assert tx["status"] == TransactionStatus.ROUTED.value

    # While ROUTED, sender funds are held in METIX_SUSPENSE
    async with session_factory() as session:
        orig = (
            await session.execute(select(Account).where(Account.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        suspense = (
            await session.execute(
                select(Account).where(Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER)
            )
        ).scalar_one()
        assert orig.balance_centavos == 50_000_000 - amount
        assert suspense.balance_centavos == amount

        # Simulate >25s elapsed past SPIM SLA deadline
        await session.execute(
            update(SwitchTransaction)
            .where(SwitchTransaction.id == tx_id)
            .values(sla_deadline_at=datetime.now(UTC) - timedelta(seconds=26))
        )
        await session.commit()

    # Run SLA sweeper
    processed = await sweeper.sweep_once()
    assert tx_id in processed

    # Verify REVERSED state, DE 39=68, 0420->0430 ISO frames with DE 90, and balance restore
    tx_after = (await client.get(f"/api/v1/transactions/{tx_id}")).json()
    assert tx_after["status"] == TransactionStatus.REVERSED.value
    assert tx_after["response_code"] == "68"

    mtis = [m["mti"] for m in tx_after["iso_messages"]]
    assert "0200" in mtis
    assert "0420" in mtis
    assert "0430" in mtis
    assert "0210" not in mtis

    msg_0420 = next(m for m in tx_after["iso_messages"] if m["mti"] == "0420")
    assert msg_0420["payload_json"]["de39"] == "68"
    assert msg_0420["payload_json"]["de90"] is not None
    assert len(msg_0420["payload_json"]["de90"]) == 42

    entry_types = {e["entry_type"] for e in tx_after["journal_entries"]}
    assert entry_types == {
        JournalEntryType.CLEARING_HOLD.value,
        JournalEntryType.REVERSAL.value,
    }

    async with session_factory() as session:
        orig = (
            await session.execute(select(Account).where(Account.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        ben = (
            await session.execute(select(Account).where(Account.account_number == MPESA_WALLET))
        ).scalar_one()
        suspense = (
            await session.execute(
                select(Account).where(Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER)
            )
        ).scalar_one()
        # Full sender restoration & zero net impact on beneficiary or suspense
        assert orig.balance_centavos == 50_000_000
        assert ben.balance_centavos == 5_000_000
        assert suspense.balance_centavos == 0


@pytest.mark.asyncio
async def test_4_individual_daily_limit_200k_mt_notice_1_gbm_2026(
    clean_env: tuple[
        httpx.AsyncClient,
        async_sessionmaker[AsyncSession],
        redis.Redis,
        object,
    ],
) -> None:
    """Scenario 4: Reject INDIVIDUAL transfers exceeding 200,000.00 MT daily cap with DE 39 = 61."""
    client, session_factory, _, _ = clean_env

    await client.post(
        "/sim/beneficiary/chaos",
        json={"preset": "normal", "latency_ms": 10, "drop_0210": False, "force_de39": None},
    )

    # First transfer: 150,000.00 MT (15,000,000 centavos) -> within 200,000.00 MT limit -> COMPLETED
    first = await client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"limit-ok-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": MPESA_WALLET,
            "amount_centavos": 15_000_000,
        },
    )
    assert first.status_code == 200
    assert first.json()["transaction"]["status"] == TransactionStatus.COMPLETED.value
    assert first.json()["transaction"]["response_code"] == "00"

    # Second transfer: 60,000.00 MT -> cumulative 210,000 MT > 200,000 MT cap -> DE 39 = 61
    second = await client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"limit-exceed-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": MPESA_WALLET,
            "amount_centavos": 6_000_000,
        },
    )
    assert second.status_code == 200
    tx2 = second.json()["transaction"]
    assert tx2["status"] == TransactionStatus.DECLINED.value
    assert tx2["response_code"] == "61"
    assert tx2["journal_entries"] == []  # No ledger mutation on pre-route limit rejection

    # Verify outbound 0210 carries DE 39 = 61
    msg_0210 = next(m for m in tx2["iso_messages"] if m["mti"] == "0210")
    assert msg_0210["response_code"] == "61"

    # Balance only reflects the first approved 150,000.00 MT transfer
    async with session_factory() as session:
        orig = (
            await session.execute(select(Account).where(Account.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        assert orig.balance_centavos == 50_000_000 - 15_000_000


@pytest.mark.asyncio
async def test_5_beneficiary_inoperative_de39_91_reverses_clearing_hold(
    clean_env: tuple[
        httpx.AsyncClient,
        async_sessionmaker[AsyncSession],
        redis.Redis,
        object,
    ],
) -> None:
    """Scenario 5: Beneficiary returns DE 39 = 91 -> DECLINED with full clearing hold reversal."""
    client, session_factory, _, _ = clean_env

    await client.post(
        "/sim/beneficiary/chaos",
        json={"preset": "inoperative_91", "latency_ms": 10},
    )

    resp = await client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"inop91-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": MPESA_WALLET,
            "amount_centavos": 300_000,
        },
    )
    assert resp.status_code == 200
    tx = resp.json()["transaction"]
    assert tx["status"] == TransactionStatus.DECLINED.value
    assert tx["response_code"] == "91"
    assert {e["entry_type"] for e in tx["journal_entries"]} == {
        JournalEntryType.CLEARING_HOLD.value,
        JournalEntryType.REVERSAL.value,
    }

    async with session_factory() as session:
        orig = (
            await session.execute(select(Account).where(Account.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        suspense = (
            await session.execute(
                select(Account).where(Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER)
            )
        ).scalar_one()
        assert orig.balance_centavos == 50_000_000
        assert suspense.balance_centavos == 0


@pytest.mark.asyncio
async def test_6_optimistic_lock_contention_solvency_and_deadlock_avoidance(
    clean_env: tuple[
        httpx.AsyncClient,
        async_sessionmaker[AsyncSession],
        redis.Redis,
        object,
    ],
) -> None:
    """Scenario 6: Verify optimistic-lock retries, solvency under contention, and deadlock safety.

    Part A: Fires 5 concurrent transfers with *distinct* idempotency keys from the same originator
    when the balance can only cover 2 of them, verifying version CAS retries, zero overdraft,
    DE 39 = 51 rejections, and released Redis daily quota.

    Part B: Fires opposite-direction transfers (A -> B and B -> A simultaneously) both end-to-end
    through SwitchEngine and directly via post_balanced_journal() behind an asyncio.Barrier,
    proving deterministic UUID lock ordering eliminates PostgreSQL circular deadlocks.
    """
    client, session_factory, _, _ = clean_env

    await client.post(
        "/sim/beneficiary/chaos",
        json={"preset": "normal", "latency_ms": 50, "drop_0210": False, "force_de39": None},
    )

    # --- Part A: 5x concurrent distinct transfers contending on a 25,000.00 MT balance ---
    starting_balance = 2_500_000  # 25,000.00 MT (covers exactly 2 x 10,000.00 MT transfers)
    transfer_amount = 1_000_000  # 10,000.00 MT each (5 x 10k = 50k MT << 200k MT daily cap)

    async with session_factory() as session:
        await session.execute(
            update(Account)
            .where(Account.account_number == BCI_INDIVIDUAL)
            .values(balance_centavos=starting_balance, version=1)
        )
        await session.commit()

    responses = await asyncio.gather(
        *(
            client.post(
                "/api/v1/transfers",
                json={
                    "idempotency_key": f"contention-{i}-{uuid.uuid4().hex}",
                    "originator_account_number": BCI_INDIVIDUAL,
                    "beneficiary_account_number": MPESA_WALLET,
                    "amount_centavos": transfer_amount,
                },
            )
            for i in range(5)
        )
    )

    for resp in responses:
        assert resp.status_code == 200
        assert resp.json()["idempotent_replay"] is False

    txs = [resp.json()["transaction"] for resp in responses]
    assert len({tx["id"] for tx in txs}) == 5

    completed = [tx for tx in txs if tx["status"] == TransactionStatus.COMPLETED.value]
    declined = [tx for tx in txs if tx["status"] == TransactionStatus.DECLINED.value]
    assert len(completed) == 2
    assert len(declined) == 3

    for tx in completed:
        assert tx["response_code"] == "00"
        assert {e["entry_type"] for e in tx["journal_entries"]} == {
            JournalEntryType.CLEARING_HOLD.value,
            JournalEntryType.SETTLEMENT.value,
        }

    for tx in declined:
        assert tx["response_code"] == "51"
        assert tx["journal_entries"] == []
        assert any(m["mti"] == "0210" and m["response_code"] == "51" for m in tx["iso_messages"])

    # Verify exact solvency (no overdraft), version increments, and released Redis daily quota
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Account).where(
                    Account.account_number.in_(
                        [BCI_INDIVIDUAL, MPESA_WALLET, METIX_SUSPENSE_ACCOUNT_NUMBER]
                    )
                )
            )
        ).scalars()
        accts = {a.account_number: a for a in rows}
        assert accts[BCI_INDIVIDUAL].balance_centavos == starting_balance - (2 * transfer_amount)
        assert accts[BCI_INDIVIDUAL].version == 3  # 1 initial + 2 approved debits
        assert accts[MPESA_WALLET].balance_centavos == 5_000_000 + (2 * transfer_amount)
        assert accts[METIX_SUSPENSE_ACCOUNT_NUMBER].balance_centavos == 0

    accounts_api = {a["account_number"]: a for a in (await client.get("/api/v1/accounts")).json()}
    assert accounts_api[BCI_INDIVIDUAL]["daily_used_centavos"] == 2 * transfer_amount

    # --- Part B1: Opposite-direction concurrent transfers end-to-end (STDB <-> EMOLA) ---
    opposite_amount = 250_000  # 2,500.00 MT each
    opposite_payloads = [
        {
            "idempotency_key": f"opp-ab-{i}-{uuid.uuid4().hex}",
            "originator_account_number": STDB_INDIVIDUAL,
            "beneficiary_account_number": EMOLA_WALLET,
            "amount_centavos": opposite_amount,
        }
        for i in range(2)
    ] + [
        {
            "idempotency_key": f"opp-ba-{i}-{uuid.uuid4().hex}",
            "originator_account_number": EMOLA_WALLET,
            "beneficiary_account_number": STDB_INDIVIDUAL,
            "amount_centavos": opposite_amount,
        }
        for i in range(2)
    ]

    opp_responses = await asyncio.gather(
        *(client.post("/api/v1/transfers", json=p) for p in opposite_payloads)
    )
    for resp in opp_responses:
        assert resp.status_code == 200
        tx = resp.json()["transaction"]
        assert tx["status"] == TransactionStatus.COMPLETED.value
        assert tx["response_code"] == "00"

    # --- Part B2: Direct opposite-direction post_balanced_journal() deadlock stress ---
    # Synchronize 4 independent DB sessions hitting (A -> B) and (B -> A) on the exact same pair
    # simultaneously to verify deterministic UUID lock ordering prevents PostgreSQL 40P01 deadlocks.
    direct_amount = 100_000  # 1,000.00 MT each
    async with session_factory() as session:
        stdb = (
            await session.execute(select(Account).where(Account.account_number == STDB_INDIVIDUAL))
        ).scalar_one()
        emola = (
            await session.execute(select(Account).where(Account.account_number == EMOLA_WALLET))
        ).scalar_one()
        assert stdb.balance_centavos == 40_000_000
        assert emola.balance_centavos == 3_500_000
        assert stdb.version == 5  # 1 initial + 2 debits + 2 credits from Part B1
        assert emola.version == 5

        direct_tx_pairs: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
        now = datetime.now(UTC)
        for idx, (debit_id, credit_id, orig_code, ben_code) in enumerate(
            [
                (stdb.id, emola.id, "STDB_MZ", "EMOLA_MZ"),
                (emola.id, stdb.id, "EMOLA_MZ", "STDB_MZ"),
                (stdb.id, emola.id, "STDB_MZ", "EMOLA_MZ"),
                (emola.id, stdb.id, "EMOLA_MZ", "STDB_MZ"),
            ]
        ):
            tx = SwitchTransaction(
                idempotency_key=f"direct-deadlock-{idx}-{uuid.uuid4().hex}",
                stan=f"99000{idx}",
                rrn=f"63262099000{idx}",
                transmission_datetime="1123120000",
                originator_code=orig_code,
                beneficiary_code=ben_code,
                originator_account_id=debit_id,
                beneficiary_account_id=credit_id,
                amount_centavos=direct_amount,
                currency="943",
                status=TransactionStatus.COMPLETED.value,
                response_code="00",
                sla_deadline_at=now + timedelta(seconds=25),
            )
            session.add(tx)
            await session.flush()
            direct_tx_pairs.append((tx.id, debit_id, credit_id))
        await session.commit()

    barrier = asyncio.Barrier(len(direct_tx_pairs))

    async def _post_direct(tx_id: uuid.UUID, debit_id: uuid.UUID, credit_id: uuid.UUID) -> None:
        async with session_factory() as worker_session:
            await barrier.wait()
            await post_balanced_journal(
                worker_session,
                transaction_id=tx_id,
                entry_type=JournalEntryType.SETTLEMENT,
                description="Opposite-direction deadlock-avoidance stress posting",
                debit_account_id=debit_id,
                credit_account_id=credit_id,
                amount_centavos=direct_amount,
            )
            await worker_session.commit()

    await asyncio.gather(*(_post_direct(*pair) for pair in direct_tx_pairs))

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Account).where(
                    Account.account_number.in_(
                        [STDB_INDIVIDUAL, EMOLA_WALLET, METIX_SUSPENSE_ACCOUNT_NUMBER]
                    )
                )
            )
        ).scalars()
        accts = {a.account_number: a for a in rows}
        assert accts[STDB_INDIVIDUAL].balance_centavos == 40_000_000
        assert accts[EMOLA_WALLET].balance_centavos == 3_500_000
        assert accts[STDB_INDIVIDUAL].version == 9  # 5 + 4 opposite-direction postings
        assert accts[EMOLA_WALLET].version == 9
        assert accts[METIX_SUSPENSE_ACCOUNT_NUMBER].balance_centavos == 0

    ledger = (await client.get("/api/v1/ledger")).json()
    assert ledger["is_balanced"] is True
    assert ledger["suspense_balance_centavos"] == 0
    expected_volume = (2 * transfer_amount * 2) + (4 * opposite_amount * 2) + (4 * direct_amount)
    assert ledger["total_debits_centavos"] == ledger["total_credits_centavos"] == expected_volume
