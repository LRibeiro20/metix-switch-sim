from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

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
from cbs.db.models import CbsAccount, CbsHold
from tests.conftest import MultiBankTestEnv

BCI_INDIVIDUAL = "00010012345678"  # Amélia Machel (500,000.00 MT in bci_cbs)
STDB_INDIVIDUAL = "00030055443322"  # Carlos Mondlane (400,000.00 MT in stdb_cbs)
STDB_SECOND = "00030077112233"  # Eduardo Chissano (250,000.00 MT in stdb_cbs)
MPESA_WALLET = "258841234567"  # Rosa Sitoe (50,000.00 MT in wallet-sim)


def test_iso8583_wire_codec_round_trip() -> None:
    """Verify canonical ISO 8583 0200 and 0420 ASCII wire frames round-trip losslessly."""
    msg_0200 = build_0200(
        originator_account=BCI_INDIVIDUAL,
        originator_code="BCI_MZ",
        beneficiary_account=STDB_INDIVIDUAL,
        beneficiary_code="STDB_MZ",
        amount_centavos=125_050,
        stan="000042",
        rrn="632620000042",
        de7="1122201500",
    )
    raw_0200 = encode_raw_iso(msg_0200)
    assert raw_0200.startswith("0200")
    assert decode_raw_iso(raw_0200) == msg_0200

    msg_0420 = build_0420(
        originator_account=BCI_INDIVIDUAL,
        originator_code="BCI_MZ",
        beneficiary_account=STDB_INDIVIDUAL,
        beneficiary_code="STDB_MZ",
        amount_centavos=125_050,
        orig_stan="000042",
        orig_rrn="632620000042",
        orig_de7="1122201500",
        response_code="68",
    )
    raw_0420 = encode_raw_iso(msg_0420)
    assert raw_0420.startswith("0420")
    decoded = decode_raw_iso(raw_0420)
    assert decoded == msg_0420
    assert decoded.de90 is not None and len(decoded.de90) == 42


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
async def test_1_happy_path_bidirectional_cbs_and_wallet_settlement(
    clean_env: MultiBankTestEnv,
) -> None:
    """Scenario 1: Happy-path BCI <-> Standard Bank & BCI -> M-Pesa across isolated DBs."""
    env = clean_env
    amount = 750_000  # 7,500.00 MT

    # 1A. BCI_MZ -> STDB_MZ across independent bci_cbs and stdb_cbs databases
    resp = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"happy-bci-stdb-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": STDB_INDIVIDUAL,
            "amount_centavos": amount,
        },
        headers=env.maker_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["idempotent_replay"] is False
    tx = body["transaction"]
    assert tx["status"] == TransactionStatus.COMPLETED.value
    assert tx["response_code"] == "00"
    assert tx["currency"] == "943"
    assert tx["originator_code"] == "BCI_MZ"
    assert tx["beneficiary_code"] == "STDB_MZ"
    assert tx["originator_hold_ref"] is not None
    assert tx["auth_id"] is not None and len(tx["auth_id"]) == 6

    # Verify ISO 8583 trace & balanced Switch settlement journals
    mtis = [(m["mti"], m["direction"]) for m in tx["iso_messages"]]
    assert ("0200", "INBOUND") in mtis
    assert ("0200", "OUTBOUND") in mtis
    assert ("0210", "INBOUND") in mtis
    assert ("0210", "OUTBOUND") in mtis

    entries = tx["journal_entries"]
    assert {e["entry_type"] for e in entries} == {
        JournalEntryType.CLEARING_HOLD.value,
        JournalEntryType.SETTLEMENT.value,
    }
    for entry in entries:
        dr = sum(
            p["amount_centavos"]
            for p in entry["postings"]
            if p["direction"] == PostingDirection.DEBIT.value
        )
        cr = sum(
            p["amount_centavos"]
            for p in entry["postings"]
            if p["direction"] == PostingDirection.CREDIT.value
        )
        assert dr == cr == amount

    # Verify isolated BCI CBS database (available == ledger == 50M - 750k, hold CAPTURED)
    async with env.bci_session_factory() as s:
        bci_acct = (
            await s.execute(select(CbsAccount).where(CbsAccount.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        hold = (
            await s.execute(select(CbsHold).where(CbsHold.hold_ref == tx["originator_hold_ref"]))
        ).scalar_one()
        assert bci_acct.available_balance_centavos == 50_000_000 - amount
        assert bci_acct.ledger_balance_centavos == 50_000_000 - amount
        assert hold.status == "CAPTURED"

    # Verify isolated Standard Bank CBS database (available == ledger == 40M + 750k)
    async with env.stdb_session_factory() as s:
        stdb_acct = (
            await s.execute(select(CbsAccount).where(CbsAccount.account_number == STDB_INDIVIDUAL))
        ).scalar_one()
        assert stdb_acct.available_balance_centavos == 40_000_000 + amount
        assert stdb_acct.ledger_balance_centavos == 40_000_000 + amount

    # Verify Switch Settlement Accounts moved from SETTLE-BCI-943 -> SETTLE-STDB-943
    async with env.switch_session_factory() as s:
        accts = {
            a.account_number: a
            for a in (
                await s.execute(
                    select(Account).where(
                        Account.account_number.in_(
                            ["SETTLE-BCI-943", "SETTLE-STDB-943", METIX_SUSPENSE_ACCOUNT_NUMBER]
                        )
                    )
                )
            ).scalars()
        }
        assert accts["SETTLE-BCI-943"].balance_centavos == 500_000_000 - amount
        assert accts["SETTLE-STDB-943"].balance_centavos == 500_000_000 + amount
        assert accts[METIX_SUSPENSE_ACCOUNT_NUMBER].balance_centavos == 0

    # 1B. Optional External Mobile Wallet Adapter (BCI_MZ -> MPESA_MZ)
    w_resp = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"happy-wallet-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": MPESA_WALLET,
            "amount_centavos": 100_000,
        },
        headers=env.maker_headers,
    )
    assert w_resp.status_code == 200
    assert w_resp.json()["transaction"]["status"] == TransactionStatus.COMPLETED.value

    ledger = (await env.client.get("/api/v1/ledger")).json()
    assert ledger["is_balanced"] is True
    assert ledger["suspense_balance_centavos"] == 0


@pytest.mark.asyncio
async def test_2_duplicate_retry_idempotency_single_execution(
    clean_env: MultiBankTestEnv,
) -> None:
    """Scenario 2: Concurrent 5x + sequential retries execute once across Switch & CBS DBs."""
    env = clean_env
    idemp_key = f"storm-{uuid.uuid4().hex}"
    amount = 500_000
    payload = {
        "idempotency_key": idemp_key,
        "originator_account_number": BCI_INDIVIDUAL,
        "beneficiary_account_number": STDB_INDIVIDUAL,
        "amount_centavos": amount,
    }

    responses = await asyncio.gather(
        *(
            env.client.post("/api/v1/transfers", json=payload, headers=env.maker_headers)
            for _ in range(5)
        )
    )
    for r in responses:
        assert r.status_code == 200

    seq_resp = await env.client.post("/api/v1/transfers", json=payload, headers=env.maker_headers)
    assert seq_resp.status_code == 200
    assert seq_resp.json()["idempotent_replay"] is True

    tx_ids = {r.json()["transaction"]["id"] for r in responses}
    tx_ids.add(seq_resp.json()["transaction"]["id"])
    assert len(tx_ids) == 1
    assert sum(1 for r in responses if r.json()["idempotent_replay"] is False) == 1

    # Verify BCI debited once and Standard Bank credited once
    async with env.bci_session_factory() as s:
        bci = (
            await s.execute(select(CbsAccount).where(CbsAccount.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        assert bci.available_balance_centavos == 50_000_000 - amount
        assert bci.ledger_balance_centavos == 50_000_000 - amount
    async with env.stdb_session_factory() as s:
        stdb = (
            await s.execute(select(CbsAccount).where(CbsAccount.account_number == STDB_INDIVIDUAL))
        ).scalar_one()
        assert stdb.available_balance_centavos == 40_000_000 + amount


@pytest.mark.asyncio
async def test_3_beneficiary_timeout_over_25s_sla_releases_cbs_hold_and_reverses(
    clean_env: MultiBankTestEnv,
) -> None:
    """Scenario 3: Dropped 0210 leaves CBS earmark hold active; >25s SLA 0420/0430 releases it."""
    env = clean_env

    # Inject drop_0210 chaos on Standard Bank CBS
    r_ch = await env.client.post(
        "/api/v1/banks/STDB_MZ/chaos",
        json={"preset": "drop_0210", "latency_ms": 10},
        headers=env.admin_headers,
    )
    assert r_ch.status_code == 200

    amount = 1_200_000  # 12,000.00 MT
    resp = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"timeout-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": STDB_INDIVIDUAL,
            "amount_centavos": amount,
        },
        headers=env.maker_headers,
    )
    assert resp.status_code == 200
    tx = resp.json()["transaction"]
    tx_id = uuid.UUID(tx["id"])
    assert tx["status"] == TransactionStatus.ROUTED.value

    # While ROUTED: BCI CBS has available < ledger (earmarked!), Switch Suspense holds amount
    async with env.bci_session_factory() as s:
        bci = (
            await s.execute(select(CbsAccount).where(CbsAccount.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        assert bci.available_balance_centavos == 50_000_000 - amount
        assert bci.ledger_balance_centavos == 50_000_000  # Ledger unchanged until capture!

    async with env.switch_session_factory() as s:
        suspense = (
            await s.execute(
                select(Account).where(Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER)
            )
        ).scalar_one()
        assert suspense.balance_centavos == amount

        # Fast-forward SLA deadline past 25s
        await s.execute(
            update(SwitchTransaction)
            .where(SwitchTransaction.id == tx_id)
            .values(sla_deadline_at=datetime.now(UTC) - timedelta(seconds=26))
        )
        await s.commit()

    processed = await env.sweeper.sweep_once()
    assert tx_id in processed

    tx_after = (await env.client.get(f"/api/v1/transactions/{tx_id}")).json()
    assert tx_after["status"] == TransactionStatus.REVERSED.value
    assert tx_after["response_code"] == "68"
    mtis = [m["mti"] for m in tx_after["iso_messages"]]
    assert "0420" in mtis and "0430" in mtis

    # Verify BCI CBS available_balance fully restored & hold RELEASED, Switch Suspense == 0
    async with env.bci_session_factory() as s:
        bci = (
            await s.execute(select(CbsAccount).where(CbsAccount.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        hold = (
            await s.execute(select(CbsHold).where(CbsHold.hold_ref == tx["originator_hold_ref"]))
        ).scalar_one()
        assert bci.available_balance_centavos == 50_000_000
        assert bci.ledger_balance_centavos == 50_000_000
        assert hold.status == "RELEASED"

    async with env.switch_session_factory() as s:
        suspense = (
            await s.execute(
                select(Account).where(Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER)
            )
        ).scalar_one()
        assert suspense.balance_centavos == 0


@pytest.mark.asyncio
async def test_4_individual_daily_limit_and_cbs_account_restrictions(
    clean_env: MultiBankTestEnv,
) -> None:
    """Scenario 4: Notice No. 1/GBM/2026 200k MT cap (DE 39=61) + CBS PND restriction (DE 39=62)."""
    env = clean_env

    # First transfer: 150,000.00 MT -> COMPLETED
    first = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"limit-ok-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": STDB_INDIVIDUAL,
            "amount_centavos": 15_000_000,
        },
        headers=env.maker_headers,
    )
    assert first.status_code == 200
    assert first.json()["transaction"]["status"] == TransactionStatus.COMPLETED.value

    # Second transfer: 60,000.00 MT -> cumulative 210,000 MT > 200,000 MT cap -> DE 39 = 61
    second = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"limit-exceed-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": STDB_INDIVIDUAL,
            "amount_centavos": 6_000_000,
        },
        headers=env.maker_headers,
    )
    assert second.status_code == 200
    tx2 = second.json()["transaction"]
    assert tx2["status"] == TransactionStatus.DECLINED.value
    assert tx2["response_code"] == "61"
    assert tx2["journal_entries"] == []

    # Place PND (Post No Debit) restriction on Standard Bank account via direct admin update
    r_pnd = await env.client.post(
        f"/api/v1/banks/STDB_MZ/accounts/{STDB_SECOND}/restrictions",
        json={"status": "PND", "reason": "KYC document expiry", "immediate": True},
        headers=env.admin_headers,
    )
    assert r_pnd.status_code == 200
    assert r_pnd.json()["account"]["status"] == "PND"

    # Attempt transfer from PND account -> declined DE 39 = 62
    r_restricted = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"pnd-test-{uuid.uuid4().hex}",
            "originator_account_number": STDB_SECOND,
            "beneficiary_account_number": BCI_INDIVIDUAL,
            "amount_centavos": 50_000,
        },
        headers=env.maker_headers,
    )
    assert r_restricted.status_code == 200
    assert r_restricted.json()["transaction"]["status"] == TransactionStatus.DECLINED.value
    assert r_restricted.json()["transaction"]["response_code"] == "62"


@pytest.mark.asyncio
async def test_5_beneficiary_inoperative_de39_91_reverses_and_releases_hold(
    clean_env: MultiBankTestEnv,
) -> None:
    """Scenario 5: Beneficiary CBS returns DE 39 = 91 -> Switch reversal & BCI hold release."""
    env = clean_env
    await env.client.post(
        "/api/v1/banks/STDB_MZ/chaos",
        json={"preset": "inoperative_91", "latency_ms": 5},
        headers=env.admin_headers,
    )

    resp = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"inop91-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": STDB_INDIVIDUAL,
            "amount_centavos": 300_000,
        },
        headers=env.maker_headers,
    )
    assert resp.status_code == 200
    tx = resp.json()["transaction"]
    assert tx["status"] == TransactionStatus.DECLINED.value
    assert tx["response_code"] == "91"
    assert {e["entry_type"] for e in tx["journal_entries"]} == {
        JournalEntryType.CLEARING_HOLD.value,
        JournalEntryType.REVERSAL.value,
    }

    async with env.bci_session_factory() as s:
        bci = (
            await s.execute(select(CbsAccount).where(CbsAccount.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        assert bci.available_balance_centavos == 50_000_000
        assert bci.ledger_balance_centavos == 50_000_000


@pytest.mark.asyncio
async def test_6_optimistic_lock_contention_solvency_and_deadlock_avoidance(
    clean_env: MultiBankTestEnv,
) -> None:
    """Scenario 6: 5x concurrent solvency contention in BCI CBS + opposite-direction safety."""
    env = clean_env

    # Part A: Set BCI customer balance to 25,000 MT -> 5x concurrent 10,000 MT transfers
    starting_bal = 2_500_000
    tx_amt = 1_000_000
    async with env.bci_session_factory() as s:
        await s.execute(
            update(CbsAccount)
            .where(CbsAccount.account_number == BCI_INDIVIDUAL)
            .values(available_balance_centavos=starting_bal, ledger_balance_centavos=starting_bal)
        )
        await s.commit()

    responses = await asyncio.gather(
        *(
            env.client.post(
                "/api/v1/transfers",
                json={
                    "idempotency_key": f"contention-{i}-{uuid.uuid4().hex}",
                    "originator_account_number": BCI_INDIVIDUAL,
                    "beneficiary_account_number": STDB_INDIVIDUAL,
                    "amount_centavos": tx_amt,
                },
                headers=env.maker_headers,
            )
            for i in range(5)
        )
    )
    txs = [r.json()["transaction"] for r in responses]
    completed = [t for t in txs if t["status"] == TransactionStatus.COMPLETED.value]
    declined = [t for t in txs if t["status"] == TransactionStatus.DECLINED.value]
    assert len(completed) == 2
    assert len(declined) == 3
    assert all(t["response_code"] == "51" for t in declined)

    async with env.bci_session_factory() as s:
        bci = (
            await s.execute(select(CbsAccount).where(CbsAccount.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        assert bci.available_balance_centavos == starting_bal - (2 * tx_amt)
        assert bci.ledger_balance_centavos == starting_bal - (2 * tx_amt)

    # Part B: Direct opposite-direction Switch ledger deadlock-avoidance stress test
    async with env.switch_session_factory() as s:
        bci_settle = (
            await s.execute(select(Account).where(Account.account_number == "SETTLE-BCI-943"))
        ).scalar_one()
        stdb_settle = (
            await s.execute(select(Account).where(Account.account_number == "SETTLE-STDB-943"))
        ).scalar_one()
        bci_before = bci_settle.balance_centavos
        stdb_before = stdb_settle.balance_centavos

    barrier = asyncio.Barrier(4)

    async def _direct(debit_id: uuid.UUID, credit_id: uuid.UUID) -> None:
        async with env.switch_session_factory() as ws:
            await barrier.wait()
            await post_balanced_journal(
                ws,
                transaction_id=None,
                entry_type=JournalEntryType.SETTLEMENT,
                description="Opposite-direction deadlock test",
                debit_account_id=debit_id,
                credit_account_id=credit_id,
                amount_centavos=100_000,
            )
            await ws.commit()

    await asyncio.gather(
        _direct(bci_settle.id, stdb_settle.id),
        _direct(stdb_settle.id, bci_settle.id),
        _direct(bci_settle.id, stdb_settle.id),
        _direct(stdb_settle.id, bci_settle.id),
    )

    async with env.switch_session_factory() as s:
        bci_after = (
            await s.execute(select(Account).where(Account.account_number == "SETTLE-BCI-943"))
        ).scalar_one()
        stdb_after = (
            await s.execute(select(Account).where(Account.account_number == "SETTLE-STDB-943"))
        ).scalar_one()
        assert bci_after.balance_centavos == bci_before
        assert stdb_after.balance_centavos == stdb_before
