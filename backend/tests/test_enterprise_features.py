from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.db.models import (
    AuditLog,
    Iso8583MessageLog,
    JournalEntry,
    SwitchTransaction,
    TransactionStatus,
)
from cbs.db.models import CbsAccount, CbsJournalEntry, CbsTransaction
from shared.pki import verify_cert_signed_by_ca
from shared.security import (
    ROLE_SCOPES,
    Principal,
    Role,
    issue_access_token,
    sign_http_payload,
)
from tests.conftest import MultiBankTestEnv

BCI_INDIVIDUAL = "00010012345678"
BCI_SECOND = "00010055001122"
BCI_BUSINESS = "00010087654321"
STDB_INDIVIDUAL = "00030055443322"
STDB_SECOND = "00030077112233"
STDB_BUSINESS = "00030099887766"


@pytest.mark.asyncio
async def test_settlement_mns_zero_sum_failure_simulation_and_signed_report(
    clean_env: MultiBankTestEnv,
) -> None:
    """Verify Multilateral Net Settlement, liquidity failure simulation, top-up & Ed25519 report."""
    env = clean_env

    # Execute bidirectional flows so netting reduces gross obligation
    r1 = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"mns-1-{uuid.uuid4().hex}",
            "originator_account_number": BCI_BUSINESS,
            "beneficiary_account_number": STDB_BUSINESS,
            "amount_centavos": 10_000_000,  # 100,000 MT BCI -> STDB
        },
        headers=env.maker_headers,
    )
    r2 = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"mns-2-{uuid.uuid4().hex}",
            "originator_account_number": STDB_INDIVIDUAL,
            "beneficiary_account_number": BCI_INDIVIDUAL,
            "amount_centavos": 4_000_000,  # 40,000 MT STDB -> BCI
        },
        headers=env.maker_headers,
    )
    assert r1.status_code == 200 and r1.json()["transaction"]["status"] == "COMPLETED"
    assert r2.status_code == 200 and r2.json()["transaction"]["status"] == "COMPLETED"

    pos = (await env.client.get("/api/v1/settlement/positions")).json()
    assert pos["netting_zero_sum_valid"] is True
    assert pos["total_gross_volume_centavos"] == 14_000_000
    assert pos["total_net_obligation_centavos"] == 6_000_000  # Netting saved 80,000 MT!

    by_bank = {p["institution_code"]: p for p in pos["positions"]}
    assert by_bank["BCI_MZ"]["net_position_centavos"] == -6_000_000
    assert by_bank["STDB_MZ"]["net_position_centavos"] == +6_000_000

    # 1. Simulate liquidity shortfall failure on BCI_MZ
    fail_cycle = await env.client.post(
        "/api/v1/settlement/cycles/run",
        json={"simulate_failure_institution": "BCI_MZ"},
        headers=env.maker_headers,
    )
    assert fail_cycle.status_code == 200
    assert fail_cycle.json()["status"] == "FAILED"

    # Transactions remain unsettled after failed cycle
    pos_after_fail = (await env.client.get("/api/v1/settlement/positions")).json()
    assert pos_after_fail["unsettled_tx_count"] == 2

    # 2. Top-up BCI RTGS liquidity and re-run MNS cycle -> SETTLED
    topup = await env.client.post(
        "/api/v1/settlement/liquidity/topup",
        json={
            "institution_code": "BCI_MZ",
            "amount_centavos": 50_000_000,
            "reason": "BM RTGS liquidity injection",
            "immediate": True,
        },
        headers=env.admin_headers,
    )
    assert topup.status_code == 200
    assert topup.json()["mode"] == "DIRECT"

    ok_cycle = await env.client.post(
        "/api/v1/settlement/cycles/run",
        json={},
        headers=env.maker_headers,
    )
    assert ok_cycle.status_code == 200
    cycle_data = ok_cycle.json()
    assert cycle_data["status"] == "SETTLED"
    assert cycle_data["total_net_settled_centavos"] == 6_000_000

    # 3. Fetch cryptographic Ed25519 settlement report and verify signature
    rep = (await env.client.get(f"/api/v1/settlement/cycles/{cycle_data['id']}/report")).json()
    assert rep["signature_algorithm"] == "Ed25519"
    assert rep["signature_valid"] is True


@pytest.mark.asyncio
async def test_three_way_reconciliation_and_maker_checker_four_eyes(
    clean_env: MultiBankTestEnv,
) -> None:
    """Verify 3-way recon detects orphans & RECON_HOLD; Maker-Checker enforces 4-eyes."""
    env = clean_env

    # 1. Create a RECON_HOLD transaction (drop 0210 + fail 0430)
    await env.client.post(
        "/api/v1/banks/STDB_MZ/chaos",
        json={"preset": "recon_hold", "latency_ms": 10},
        headers=env.admin_headers,
    )
    r_tx = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"recon-hold-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": STDB_INDIVIDUAL,
            "amount_centavos": 450_000,
        },
        headers=env.maker_headers,
    )
    tx_id = uuid.UUID(r_tx.json()["transaction"]["id"])
    async with env.switch_session_factory() as s:
        await s.execute(
            update(SwitchTransaction)
            .where(SwitchTransaction.id == tx_id)
            .values(sla_deadline_at=datetime.now(UTC) - timedelta(seconds=30))
        )
        await s.commit()
    await env.sweeper.sweep_once()

    tx_hold = (await env.client.get(f"/api/v1/transactions/{tx_id}")).json()
    assert tx_hold["status"] == TransactionStatus.RECON_HOLD.value

    # 2. Inject an ORPHAN_CBS_POSTING in Standard Bank CBS
    r_flt = await env.client.post(
        "/api/v1/reconciliation/inject-fault",
        json={
            "bank_code": "STDB_MZ",
            "fault_type": "ORPHAN_CBS_POSTING",
            "account_number": STDB_INDIVIDUAL,
            "amount_centavos": 175_000,
        },
        headers=env.maker_headers,
    )
    assert r_flt.status_code == 200

    # 3. Run 3-Way Reconciliation
    r_recon = await env.client.post("/api/v1/reconciliation/run", headers=env.maker_headers)
    assert r_recon.status_code == 200
    recon_data = r_recon.json()
    exc_types = {e["exception_type"] for e in recon_data["exceptions"]}
    assert "INCOMPLETE_TRANSFER" in exc_types
    assert "MISSING_IN_SWITCH" in exc_types
    assert "SUSPENSE_AGING" in exc_types

    # Locate the INCOMPLETE_TRANSFER exception tied to tx_id
    hold_exc = next(e for e in recon_data["exceptions"] if e["switch_tx_id"] == str(tx_id))

    # 4. Maker proposes suspense release adjustment
    r_prop = await env.client.post(
        f"/api/v1/reconciliation/exceptions/{hold_exc['id']}/adjust",
        json={
            "adjustment_mode": "RELEASE_SUSPENSE_TO_ORIGINATOR",
            "reason": "Confirmed beneficiary never credited; releasing suspense to BCI",
        },
        headers=env.maker_headers,
    )
    assert r_prop.status_code == 200
    mc_id = r_prop.json()["id"]

    # 5. Maker CANNOT approve their own proposal (4-eyes RBAC + self-approval block)
    r_self = await env.client.post(
        f"/api/v1/security/maker-checker/{mc_id}/decide",
        json={"approve": True, "notes": "Trying self-approval"},
        headers=env.maker_headers,
    )
    assert r_self.status_code == 403

    # Even Admin proposing and trying to self-approve must get 403!
    r_admin_prop = await env.client.post(
        f"/api/v1/banks/BCI_MZ/accounts/{BCI_INDIVIDUAL}/restrictions",
        json={"status": "PND", "reason": "Test self-approval block", "immediate": False},
        headers=env.admin_headers,
    )
    admin_mc_id = r_admin_prop.json()["request"]["id"]
    r_admin_self = await env.client.post(
        f"/api/v1/security/maker-checker/{admin_mc_id}/decide",
        json={"approve": True, "notes": "Admin self approval must fail"},
        headers=env.admin_headers,
    )
    assert r_admin_self.status_code == 403

    # 6. Checker approves Maker's adjustment -> Suspense clears to 0 & ledger remains balanced
    r_app = await env.client.post(
        f"/api/v1/security/maker-checker/{mc_id}/decide",
        json={"approve": True, "notes": "Verified against STDB CBS export"},
        headers=env.checker_headers,
    )
    assert r_app.status_code == 200
    assert r_app.json()["status"] == "APPROVED"

    ledger = (await env.client.get("/api/v1/ledger")).json()
    assert ledger["is_balanced"] is True
    assert ledger["suspense_balance_centavos"] == 0


@pytest.mark.asyncio
async def test_fraud_round_trip_review_queue_and_velocity_burst(
    clean_env: MultiBankTestEnv,
) -> None:
    """Verify rapid round-trip triggers Fraud Review Queue & velocity burst triggers detection."""
    env = clean_env

    # 1. Leg A->B (10,000 MT) clears normally
    r1 = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"rt-1-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": STDB_INDIVIDUAL,
            "amount_centavos": 1_000_000,
        },
        headers=env.maker_headers,
    )
    assert r1.status_code == 200
    assert r1.json()["transaction"]["status"] == TransactionStatus.COMPLETED.value

    # 2. Immediate reverse corridor B->A for same amount -> flagged for Fraud Review Queue!
    r2 = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"rt-2-{uuid.uuid4().hex}",
            "originator_account_number": STDB_INDIVIDUAL,
            "beneficiary_account_number": BCI_INDIVIDUAL,
            "amount_centavos": 1_000_000,
        },
        headers=env.maker_headers,
    )
    assert r2.status_code == 200
    tx2 = r2.json()["transaction"]
    assert tx2["status"] == TransactionStatus.INITIATED.value
    assert tx2["fraud_status"] == "PENDING_REVIEW"
    assert tx2["fraud_score"] >= 50

    cases = (await env.client.get("/api/v1/fraud/cases")).json()
    assert len(cases) >= 1
    case = cases[0]
    assert case["status"] == "PENDING_REVIEW"
    assert any(rule["rule"] == "RAPID_ROUND_TRIP_LAYERING" for rule in case["triggered_rules"])

    # 3. Fraud Analyst releases the reviewed transaction -> settles cleanly & records audit trail
    r_rel = await env.client.post(
        f"/api/v1/fraud/cases/{case['id']}/decision",
        json={"decision": "RELEASE", "notes": "Customer confirmed legitimate treasury refund"},
        headers=env.fraud_headers,
    )
    assert r_rel.status_code == 200
    assert r_rel.json()["transaction_status"] == TransactionStatus.COMPLETED.value

    # 4. Velocity burst on BCI_BUSINESS (8 rapid transfers in 60s triggers VELOCITY_1M_BURST)
    burst_scores = []
    for i in range(8):
        rb = await env.client.post(
            "/api/v1/transfers",
            json={
                "idempotency_key": f"vel-{i}-{uuid.uuid4().hex}",
                "originator_account_number": BCI_BUSINESS,
                "beneficiary_account_number": STDB_BUSINESS,
                "amount_centavos": 50_000,
            },
            headers=env.maker_headers,
        )
        assert rb.status_code == 200
        burst_scores.append(rb.json()["transaction"]["fraud_score"])
    assert max(burst_scores) >= 55


@pytest.mark.asyncio
async def test_enterprise_security_mtls_oauth_rbac_signatures_encryption_and_audit_chain(
    clean_env: MultiBankTestEnv,
) -> None:
    """Verify mTLS PKI, OAuth 2.0 RBAC, Ed25519 anti-tamper/replay, AES-GCM PII & Audit Chain."""
    env = clean_env

    # 1. Mutual TLS X.509 PKI chain verification
    for svc in ("switch-api", "bci-cbs", "stdb-cbs", "wallet-sim"):
        assert verify_cert_signed_by_ca(env.certs_dir, svc) is True

    # 2. OAuth 2.0 & RBAC enforcement (401 without token, 403 for OPS_VIEWER)
    r_no_auth = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"noauth-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": STDB_INDIVIDUAL,
            "amount_centavos": 10_000,
        },
    )
    assert r_no_auth.status_code == 401

    r_viewer = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": f"viewer-{uuid.uuid4().hex}",
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": STDB_INDIVIDUAL,
            "amount_centavos": 10_000,
        },
        headers=env.viewer_headers,
    )
    assert r_viewer.status_code == 403

    # 3. Ed25519 Digital Signature tamper & replay rejection directly against bci-cbs
    svc_principal = Principal(
        subject="switch-api",
        display_name="METIX Central Switch",
        role=Role.SERVICE_ADAPTER,
        scopes=ROLE_SCOPES[Role.SERVICE_ADAPTER],
    )
    svc_jwt, _ = issue_access_token(svc_principal, jwt_secret=env.settings.jwt_secret)
    body_obj = {
        "status": "PND",
        "reason": "Signature verification test",
    }
    raw_body = json.dumps(body_obj, separators=(",", ":")).encode()
    path = f"/cbs/v1/accounts/{BCI_INDIVIDUAL}/restrictions"
    sig_headers = sign_http_payload(
        method="POST",
        path=path,
        body_bytes=raw_body,
        service_id="switch-api",
        master_seed=env.settings.signing_master_seed,
    )
    headers = {
        "Authorization": f"Bearer {svc_jwt}",
        "Content-Type": "application/json",
        **sig_headers,
    }

    # Tamper body after signing -> must be rejected with 401
    tampered = json.dumps(
        {"status": "FROZEN", "reason": "Tampered!"}, separators=(",", ":")
    ).encode()
    r_tamper = await env.bci_client.post(path, content=tampered, headers=headers)
    assert r_tamper.status_code == 401

    # Valid signed request succeeds once
    r_valid = await env.bci_client.post(path, content=raw_body, headers=headers)
    assert r_valid.status_code == 200

    # Replaying exact same signed request (same X-Nonce) -> rejected with 401 Replay Detected!
    r_replay = await env.bci_client.post(path, content=raw_body, headers=headers)
    assert r_replay.status_code == 401

    # 4. AES-256-GCM encryption at rest in bci_cbs database
    async with env.bci_session_factory() as s:
        raw_acct = (
            await s.execute(select(CbsAccount).where(CbsAccount.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        assert raw_acct.holder_name_enc.startswith("enc:v1:")
        assert "Amélia" not in raw_acct.holder_name_enc

    # 5. Hash-Chained Immutable Audit Log integrity & tamper detection
    v_ok = await env.client.post("/api/v1/security/audit-logs/verify", headers=env.viewer_headers)
    assert v_ok.status_code == 200
    assert v_ok.json()["valid"] is True
    assert v_ok.json()["checked_entries"] >= 5

    # Simulate direct DB tampering on sequence #1 -> chain verification MUST catch it
    async with env.switch_session_factory() as s:
        await s.execute(
            update(AuditLog).where(AuditLog.sequence_no == 1).values(action="TAMPERED_ACTION")
        )
        await s.commit()

    v_bad = await env.client.post("/api/v1/security/audit-logs/verify", headers=env.viewer_headers)
    assert v_bad.status_code == 200
    assert v_bad.json()["valid"] is False
    assert v_bad.json()["broken_at_sequence"] == 1


@pytest.mark.asyncio
async def test_same_bank_on_us_transfer_bypasses_metix_spim(
    clean_env: MultiBankTestEnv,
) -> None:
    """Verify same-bank transfers execute directly in CBS without METIX/SPIM, ISO 8583, or MNS."""
    env = clean_env
    amount = 1_250_000  # 12,500.00 MT

    # Enable ISO 8583 chaos (drop_0210) on BCI_MZ — must NOT affect on-us book transfers!
    chaos_res = await env.client.post(
        "/api/v1/banks/BCI_MZ/chaos",
        json={"preset": "drop_0210"},
        headers=env.maker_headers,
    )
    assert chaos_res.status_code == 200

    # 1. Transparent proxy via POST /api/v1/transfers (BCI_MZ -> BCI_MZ)
    idemp_key = f"onus-bci-{uuid.uuid4().hex}"
    resp = await env.client.post(
        "/api/v1/transfers",
        json={
            "idempotency_key": idemp_key,
            "originator_account_number": BCI_INDIVIDUAL,
            "beneficiary_account_number": BCI_SECOND,
            "amount_centavos": amount,
        },
        headers=env.maker_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["routing_rail"] == "CBS_ON_US"
    assert body["idempotent_replay"] is False
    assert body["transaction"] is None
    on_us = body["on_us_transfer"]
    assert on_us is not None
    assert on_us["approved"] is True
    assert on_us["bank_code"] == "BCI_MZ"
    assert on_us["response_code"] == "00"
    assert on_us["auth_id"] is not None and len(on_us["auth_id"]) == 6
    assert on_us["cbs_ref"].startswith("ONUS-BCI_MZ-")
    assert on_us["originator_available_balance_centavos"] == 50_000_000 - amount
    assert on_us["originator_ledger_balance_centavos"] == 50_000_000 - amount
    assert on_us["beneficiary_available_balance_centavos"] == 30_000_000 + amount
    assert on_us["beneficiary_ledger_balance_centavos"] == 30_000_000 + amount

    # Verify zero METIX/SPIM switch artifacts (no SwitchTransaction, ISO 8583, or Switch Journals)
    async with env.switch_session_factory() as s:
        assert len((await s.execute(select(SwitchTransaction))).scalars().all()) == 0
        assert len((await s.execute(select(Iso8583MessageLog))).scalars().all()) == 0
        assert len((await s.execute(select(JournalEntry))).scalars().all()) == 0

    # Verify MNS settlement is completely untouched
    pos = (await env.client.get("/api/v1/settlement/positions")).json()
    assert pos["unsettled_tx_count"] == 0
    assert pos["total_gross_volume_centavos"] == 0

    # Verify BCI CBS DB recorded balanced ON_US journal and left Clearing GL at 0
    async with env.bci_session_factory() as s:
        gl = (
            await s.execute(
                select(CbsAccount).where(CbsAccount.account_number == "BCI-CLEARING-GL-943")
            )
        ).scalar_one()
        assert gl.ledger_balance_centavos == 0
        assert gl.available_balance_centavos == 0
        cbs_txs = (await s.execute(select(CbsTransaction))).scalars().all()
        assert {t.tx_type for t in cbs_txs} == {"ON_US_DEBIT", "ON_US_CREDIT"}
        assert all(t.switch_tx_id is None for t in cbs_txs)
        jes = (await s.execute(select(CbsJournalEntry))).scalars().all()
        assert len(jes) == 1 and jes[0].entry_type == "ON_US_TRANSFER"

    # 2. Concurrent 5x idempotency storm on same-bank transfer via transparent proxy
    storm_key = f"onus-storm-{uuid.uuid4().hex}"
    storm_amount = 500_000  # 5,000.00 MT
    storm_responses = await asyncio.gather(
        *[
            env.client.post(
                "/api/v1/transfers",
                json={
                    "idempotency_key": storm_key,
                    "originator_account_number": BCI_INDIVIDUAL,
                    "beneficiary_account_number": BCI_SECOND,
                    "amount_centavos": storm_amount,
                },
                headers=env.maker_headers,
            )
            for _ in range(5)
        ]
    )
    assert all(r.status_code == 200 for r in storm_responses)
    storm_bodies = [r.json() for r in storm_responses]
    assert all(b["routing_rail"] == "CBS_ON_US" for b in storm_bodies)
    assert sum(1 for b in storm_bodies if not b["idempotent_replay"]) == 1
    assert sum(1 for b in storm_bodies if b["idempotent_replay"]) == 4
    assert len({b["on_us_transfer"]["cbs_ref"] for b in storm_bodies}) == 1

    # Confirm only 1 storm debit occurred in bci_cbs
    async with env.bci_session_factory() as s:
        orig = (
            await s.execute(select(CbsAccount).where(CbsAccount.account_number == BCI_INDIVIDUAL))
        ).scalar_one()
        ben = (
            await s.execute(select(CbsAccount).where(CbsAccount.account_number == BCI_SECOND))
        ).scalar_one()
        assert orig.available_balance_centavos == 50_000_000 - amount - storm_amount
        assert ben.available_balance_centavos == 30_000_000 + amount + storm_amount

    # 3. Direct bank endpoint POST /api/v1/banks/STDB_MZ/transfers/internal + cross-bank guard
    stdb_res = await env.client.post(
        "/api/v1/banks/STDB_MZ/transfers/internal",
        json={
            "idempotency_key": f"onus-stdb-{uuid.uuid4().hex}",
            "originator_account_number": STDB_INDIVIDUAL,
            "beneficiary_account_number": STDB_SECOND,
            "amount_centavos": 300_000,
        },
        headers=env.maker_headers,
    )
    assert stdb_res.status_code == 200, stdb_res.text
    assert stdb_res.json()["approved"] is True
    assert stdb_res.json()["bank_code"] == "STDB_MZ"

    bad_cross = await env.client.post(
        "/api/v1/banks/STDB_MZ/transfers/internal",
        json={
            "idempotency_key": f"onus-cross-{uuid.uuid4().hex}",
            "originator_account_number": STDB_INDIVIDUAL,
            "beneficiary_account_number": BCI_INDIVIDUAL,
            "amount_centavos": 100_000,
        },
        headers=env.maker_headers,
    )
    assert bad_cross.status_code == 400

    # 4. 3-Way Reconciliation must NOT flag on-us transfers as MISSING_IN_SWITCH
    recon_res = await env.client.post("/api/v1/reconciliation/run", headers=env.maker_headers)
    assert recon_res.status_code == 200
    assert recon_res.json()["run"]["new_exception_count"] == 0
    assert recon_res.json()["exceptions"] == []
