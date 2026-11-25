from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.adapters.registry import AdapterRegistry
from app.api.schemas import (
    BankChaosUpdateRequest,
    BankRestrictionProposalRequest,
    FraudDecisionRequest,
    LiquidityTopupRequest,
    ReconAdjustProposeRequest,
    ReconFaultProxyRequest,
    SettlementRunRequest,
    ShortfallToggleRequest,
)
from app.db.models import FraudCase, FraudInvestigationEvent, SystemAlert
from app.services.audit import record_audit_log
from app.services.maker_checker import MakerCheckerService
from app.services.reconciliation import ReconciliationService
from app.services.redis_guard import RedisGuard
from app.services.settlement import SettlementService
from app.services.switch_engine import SwitchEngine
from app.services.telemetry import TelemetryService
from shared.schemas import (
    BankChaosConfig,
    CbsAccountStatus,
    CbsRestrictionUpdateRequest,
    ReconFaultInjectRequest,
)
from shared.security import Principal, Role, require_roles

router = APIRouter(prefix="/api/v1", tags=["banking-operations"])

CHAOS_PRESETS: dict[str, BankChaosConfig] = {
    "normal": BankChaosConfig(
        preset="normal",
        latency_ms=60,
        drop_0210=False,
        force_de39=None,
        fail_0430=False,
        offline=False,
    ),
    "slow_15s": BankChaosConfig(
        preset="slow_15s",
        latency_ms=15_000,
        drop_0210=False,
        force_de39=None,
        fail_0430=False,
        offline=False,
    ),
    "drop_0210": BankChaosConfig(
        preset="drop_0210",
        latency_ms=100,
        drop_0210=True,
        force_de39=None,
        fail_0430=False,
        offline=False,
    ),
    "inoperative_91": BankChaosConfig(
        preset="inoperative_91",
        latency_ms=120,
        drop_0210=False,
        force_de39="91",
        fail_0430=False,
        offline=False,
    ),
    "recon_hold": BankChaosConfig(
        preset="recon_hold",
        latency_ms=100,
        drop_0210=True,
        force_de39=None,
        fail_0430=True,
        offline=False,
    ),
    "offline": BankChaosConfig(
        preset="offline",
        latency_ms=0,
        drop_0210=False,
        force_de39=None,
        fail_0430=False,
        offline=True,
    ),
}


# ---------------------------------------------------------------------------
# 1. Core Banking Simulators & Bank Adapter Connectivity
# ---------------------------------------------------------------------------


@router.get("/banks/connectivity")
async def get_bank_connectivity(request: Request) -> list[dict[str, Any]]:
    registry: AdapterRegistry = request.app.state.adapter_registry
    statuses = await registry.check_all_connectivity()
    return [asdict(s) for s in statuses]


@router.get("/banks/accounts")
async def list_all_bank_customer_accounts(request: Request) -> list[dict[str, Any]]:
    """Aggregate live customer accounts (Available vs Ledger) from BCI, STDB & Wallets."""
    registry: AdapterRegistry = request.app.state.adapter_registry
    guard = RedisGuard(request.app.state.redis, request.app.state.settings)
    accounts = await registry.list_all_customer_accounts()
    out: list[dict[str, Any]] = []
    for acct in accounts:
        daily_ref = f"{acct.bank_code}:{acct.account_number}"
        switch_used = await guard.get_daily_usage(daily_ref)
        d = acct.model_dump(mode="json")
        d["switch_daily_used_centavos"] = switch_used
        d["switch_daily_limit_centavos"] = guard.daily_limit_for_tier(acct.account_tier)
        out.append(d)
    return out


@router.get("/banks/{bank_code}/accounts")
async def list_single_bank_accounts(bank_code: str, request: Request) -> list[dict[str, Any]]:
    registry: AdapterRegistry = request.app.state.adapter_registry
    try:
        adapter = registry.get(bank_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    accts = await adapter.list_accounts()
    return [a.model_dump(mode="json") for a in accts]


@router.post("/banks/{bank_code}/accounts/{account_number}/restrictions")
async def propose_or_apply_account_restriction(
    bank_code: str,
    account_number: str,
    payload: BankRestrictionProposalRequest,
    request: Request,
    actor: Principal = Depends(require_roles(Role.MAKER, Role.ADMIN)),
) -> dict[str, Any]:
    """Propose (Maker-Checker) or immediately apply (Admin) CBS account restrictions/limits."""
    registry: AdapterRegistry = request.app.state.adapter_registry
    try:
        adapter = registry.get(bank_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if payload.immediate and actor.role == Role.ADMIN:
        req = CbsRestrictionUpdateRequest(
            status=CbsAccountStatus(payload.status) if payload.status else None,
            per_tx_limit_centavos=payload.per_tx_limit_centavos,
            daily_limit_centavos=payload.daily_limit_centavos,
            reason=payload.reason,
        )
        updated = await adapter.update_restrictions(account_number, req)
        async with request.app.state.session_factory() as session:
            await record_audit_log(
                session,
                actor_id=actor.subject,
                actor_role=actor.role.value,
                action="CBS_RESTRICTION_DIRECT_UPDATE",
                resource_type="cbs_account",
                resource_id=f"{bank_code}:{account_number}",
                payload_json=payload.model_dump(mode="json", exclude_none=True),
            )
            await session.commit()
        return {"mode": "DIRECT", "account": updated.model_dump(mode="json")}

    mc: MakerCheckerService = request.app.state.maker_checker_service
    mc_req = await mc.propose(
        action_type="ACCOUNT_RESTRICTION",
        resource_type="cbs_account",
        resource_id=f"{bank_code}:{account_number}",
        payload_json={
            "bank_code": bank_code,
            "account_number": account_number,
            "status": payload.status,
            "per_tx_limit_centavos": payload.per_tx_limit_centavos,
            "daily_limit_centavos": payload.daily_limit_centavos,
            "reason": payload.reason,
        },
        reason=payload.reason,
        maker=actor,
    )
    return {"mode": "MAKER_CHECKER_PENDING", "request": mc_req}


@router.get("/banks/{bank_code}/chaos")
async def get_bank_chaos(bank_code: str, request: Request) -> BankChaosConfig:
    registry: AdapterRegistry = request.app.state.adapter_registry
    try:
        adapter = registry.get(bank_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await adapter.get_chaos()


@router.post("/banks/{bank_code}/chaos")
async def update_bank_chaos(
    bank_code: str,
    payload: BankChaosUpdateRequest,
    request: Request,
    actor: Principal = Depends(require_roles(Role.MAKER, Role.ADMIN)),
) -> BankChaosConfig:
    registry: AdapterRegistry = request.app.state.adapter_registry
    try:
        adapter = registry.get(bank_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if payload.preset and payload.preset in CHAOS_PRESETS:
        base = CHAOS_PRESETS[payload.preset].model_copy(deep=True)
    else:
        base = await adapter.get_chaos()
        if payload.preset is not None:
            base.preset = payload.preset
    updates = payload.model_dump(exclude_unset=True, exclude={"preset"})
    merged = base.model_copy(update=updates)
    result = await adapter.set_chaos(merged)

    async with request.app.state.session_factory() as session:
        await record_audit_log(
            session,
            actor_id=actor.subject,
            actor_role=actor.role.value,
            action="BANK_CHAOS_UPDATED",
            resource_type="bank_adapter",
            resource_id=bank_code,
            payload_json=result.model_dump(mode="json"),
        )
        await session.commit()
    return result


# ---------------------------------------------------------------------------
# 2. Interbank Clearing & Multilateral Net Settlement
# ---------------------------------------------------------------------------


@router.get("/settlement/positions")
async def get_settlement_positions(request: Request) -> dict[str, Any]:
    svc: SettlementService = request.app.state.settlement_service
    return await svc.get_live_positions()


@router.get("/settlement/cycles")
async def list_settlement_cycles(
    request: Request, limit: int = Query(default=20, ge=1, le=100)
) -> list[dict[str, Any]]:
    svc: SettlementService = request.app.state.settlement_service
    cycles = await svc.list_cycles(limit=limit)
    return [
        {
            "id": str(c.id),
            "cycle_number": c.cycle_number,
            "status": c.status,
            "total_gross_volume_centavos": c.total_gross_volume_centavos,
            "total_net_settled_centavos": c.total_net_settled_centavos,
            "tx_count": c.tx_count,
            "failure_reason": c.failure_reason,
            "initiated_by": c.initiated_by,
            "report_signature": c.report_signature,
            "created_at": c.created_at.isoformat(),
            "settled_at": c.settled_at.isoformat() if c.settled_at else None,
            "positions": [
                {
                    "institution_code": p.institution_code,
                    "gross_debits_centavos": p.gross_debits_centavos,
                    "gross_credits_centavos": p.gross_credits_centavos,
                    "net_position_centavos": p.net_position_centavos,
                    "tx_count": p.tx_count,
                    "opening_balance_centavos": p.opening_balance_centavos,
                    "closing_balance_centavos": p.closing_balance_centavos,
                    "available_liquidity_centavos": p.available_liquidity_centavos,
                    "liquidity_utilization_bps": p.liquidity_utilization_bps,
                    "status": p.status,
                }
                for p in c.positions
            ],
        }
        for c in cycles
    ]


@router.post("/settlement/cycles/run")
async def run_settlement_cycle(
    payload: SettlementRunRequest,
    request: Request,
    actor: Principal = Depends(require_roles(Role.MAKER, Role.CHECKER, Role.ADMIN)),
) -> dict[str, Any]:
    svc: SettlementService = request.app.state.settlement_service
    c = await svc.execute_mns_cycle(
        actor=actor,
        simulate_failure_institution=payload.simulate_failure_institution,
    )
    return {
        "id": str(c.id),
        "cycle_number": c.cycle_number,
        "status": c.status,
        "total_gross_volume_centavos": c.total_gross_volume_centavos,
        "total_net_settled_centavos": c.total_net_settled_centavos,
        "tx_count": c.tx_count,
        "failure_reason": c.failure_reason,
        "report_signature": c.report_signature,
    }


@router.post("/settlement/simulate-shortfall")
async def toggle_settlement_shortfall(
    payload: ShortfallToggleRequest,
    request: Request,
    actor: Principal = Depends(require_roles(Role.MAKER, Role.ADMIN)),
) -> dict[str, Any]:
    svc: SettlementService = request.app.state.settlement_service
    try:
        return await svc.set_shortfall_simulation(
            payload.institution_code, enabled=payload.enabled, actor=actor
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/settlement/liquidity/topup")
async def topup_settlement_liquidity(
    payload: LiquidityTopupRequest,
    request: Request,
    actor: Principal = Depends(require_roles(Role.MAKER, Role.CHECKER, Role.ADMIN)),
) -> dict[str, Any]:
    if payload.immediate and actor.role in (Role.CHECKER, Role.ADMIN):
        svc: SettlementService = request.app.state.settlement_service
        try:
            res = await svc.topup_liquidity(
                institution_code=payload.institution_code,
                amount_centavos=payload.amount_centavos,
                actor=actor,
                reason=payload.reason,
            )
            return {"mode": "DIRECT", "result": res}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    mc: MakerCheckerService = request.app.state.maker_checker_service
    req = await mc.propose(
        action_type="LIQUIDITY_TOPUP",
        resource_type="settlement_account",
        resource_id=payload.institution_code,
        payload_json={
            "institution_code": payload.institution_code,
            "amount_centavos": payload.amount_centavos,
        },
        reason=payload.reason,
        maker=actor,
    )
    return {"mode": "MAKER_CHECKER_PENDING", "request": req}


@router.get("/settlement/cycles/{cycle_id}/report")
async def get_settlement_report(cycle_id: uuid.UUID, request: Request) -> dict[str, Any]:
    svc: SettlementService = request.app.state.settlement_service
    try:
        return await svc.get_signed_report(cycle_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 3. 3-Way Financial Reconciliation & Suspense Management
# ---------------------------------------------------------------------------


@router.post("/reconciliation/run")
async def run_reconciliation(
    request: Request,
    actor: Principal = Depends(require_roles(Role.MAKER, Role.CHECKER, Role.ADMIN)),
) -> dict[str, Any]:
    svc: ReconciliationService = request.app.state.reconciliation_service
    return await svc.run_three_way_reconciliation(actor=actor)


@router.get("/reconciliation/exceptions")
async def list_reconciliation_exceptions(
    request: Request, limit: int = Query(default=100, ge=1, le=200)
) -> list[dict[str, Any]]:
    svc: ReconciliationService = request.app.state.reconciliation_service
    return await svc.list_exceptions(limit=limit)


@router.get("/reconciliation/suspense")
async def get_suspense_breakdown(request: Request) -> dict[str, Any]:
    svc: ReconciliationService = request.app.state.reconciliation_service
    return await svc.get_suspense_breakdown()


@router.post("/reconciliation/exceptions/{exception_id}/adjust")
async def propose_reconciliation_adjustment(
    exception_id: uuid.UUID,
    payload: ReconAdjustProposeRequest,
    request: Request,
    maker: Principal = Depends(require_roles(Role.MAKER, Role.ADMIN)),
) -> dict[str, Any]:
    mc: MakerCheckerService = request.app.state.maker_checker_service
    return await mc.propose(
        action_type="RECON_ADJUSTMENT",
        resource_type="reconciliation_exception",
        resource_id=str(exception_id),
        payload_json={"adjustment_mode": payload.adjustment_mode},
        reason=payload.reason,
        maker=maker,
    )


@router.post("/reconciliation/inject-fault")
async def inject_reconciliation_fault(
    payload: ReconFaultProxyRequest,
    request: Request,
    actor: Principal = Depends(require_roles(Role.MAKER, Role.ADMIN)),
) -> dict[str, Any]:
    registry: AdapterRegistry = request.app.state.adapter_registry
    adapter = registry.get(payload.bank_code)
    created = await adapter.inject_recon_fault(
        ReconFaultInjectRequest(
            fault_type=payload.fault_type,
            account_number=payload.account_number,
            amount_centavos=payload.amount_centavos,
            switch_tx_id=payload.switch_tx_id,
            stan=payload.stan,
            rrn=payload.rrn,
        )
    )
    async with request.app.state.session_factory() as session:
        await record_audit_log(
            session,
            actor_id=actor.subject,
            actor_role=actor.role.value,
            action="RECON_FAULT_INJECTED",
            resource_type="cbs_transaction",
            resource_id=created.cbs_ref,
            payload_json=payload.model_dump(mode="json"),
        )
        await session.commit()
    return created.model_dump(mode="json")


# ---------------------------------------------------------------------------
# 4. Fraud Detection Review Queue & Investigation Audit Trail
# ---------------------------------------------------------------------------


@router.get("/fraud/cases")
async def list_fraud_cases(
    request: Request, limit: int = Query(default=50, ge=1, le=200)
) -> list[dict[str, Any]]:
    async with request.app.state.session_factory() as session:
        cases = list(
            (
                await session.execute(
                    select(FraudCase)
                    .options(selectinload(FraudCase.events))
                    .order_by(FraudCase.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": str(c.id),
            "transaction_id": str(c.transaction_id),
            "originator_code": c.originator_code,
            "originator_account_number": c.originator_account_number,
            "beneficiary_code": c.beneficiary_code,
            "beneficiary_account_number": c.beneficiary_account_number,
            "amount_centavos": c.amount_centavos,
            "risk_score": c.risk_score,
            "triggered_rules": c.triggered_rules,
            "status": c.status,
            "assigned_to": c.assigned_to,
            "decision_notes": c.decision_notes,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
            "events": [
                {
                    "id": str(e.id),
                    "actor_id": e.actor_id,
                    "actor_role": e.actor_role,
                    "action": e.action,
                    "notes": e.notes,
                    "metadata_json": e.metadata_json,
                    "created_at": e.created_at.isoformat(),
                }
                for e in c.events
            ],
        }
        for c in cases
    ]


@router.post("/fraud/cases/{case_id}/decision")
async def decide_fraud_case(
    case_id: uuid.UUID,
    payload: FraudDecisionRequest,
    request: Request,
    analyst: Principal = Depends(require_roles(Role.FRAUD_ANALYST, Role.CHECKER, Role.ADMIN)),
) -> dict[str, Any]:
    engine: SwitchEngine = request.app.state.switch_engine
    async with request.app.state.session_factory() as session:
        fcase = (
            await session.execute(
                select(FraudCase).where(FraudCase.id == case_id).with_for_update()
            )
        ).scalar_one_or_none()
        if fcase is None:
            raise HTTPException(status_code=404, detail="Fraud case not found")
        if fcase.status != "PENDING_REVIEW":
            raise HTTPException(
                status_code=409,
                detail=f"Fraud case already decided ({fcase.status})",
            )
        tx_id = fcase.transaction_id
        fcase.assigned_to = analyst.subject
        fcase.decision_notes = payload.notes
        fcase.status = "RELEASED" if payload.decision == "RELEASE" else "CONFIRMED_FRAUD"
        session.add(
            FraudInvestigationEvent(
                fraud_case_id=fcase.id,
                actor_id=analyst.subject,
                actor_role=analyst.role.value,
                action=f"ANALYST_{payload.decision}",
                notes=payload.notes,
                metadata_json={"decision": payload.decision},
            )
        )
        await session.commit()

    if payload.decision == "RELEASE":
        tx = await engine.resume_reviewed_transfer(
            tx_id, actor_id=analyst.subject, actor_role=analyst.role.value
        )
    else:
        tx = await engine.reject_reviewed_transfer(
            tx_id, actor_id=analyst.subject, actor_role=analyst.role.value, notes=payload.notes
        )

    return {
        "case_id": str(case_id),
        "decision": payload.decision,
        "transaction_id": str(tx.id),
        "transaction_status": tx.status,
        "response_code": tx.response_code,
    }


# ---------------------------------------------------------------------------
# 5. Operations Telemetry & System Alerts
# ---------------------------------------------------------------------------


@router.get("/ops/telemetry")
async def get_ops_telemetry(request: Request) -> dict[str, Any]:
    svc: TelemetryService = request.app.state.telemetry_service
    return await svc.get_ops_telemetry()


@router.get("/ops/alerts")
async def list_ops_alerts(
    request: Request, limit: int = Query(default=40, ge=1, le=200)
) -> list[dict[str, Any]]:
    async with request.app.state.session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(SystemAlert).order_by(SystemAlert.created_at.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": str(a.id),
            "category": a.category,
            "severity": a.severity,
            "title": a.title,
            "message": a.message,
            "source_service": a.source_service,
            "resource_id": a.resource_id,
            "acknowledged": a.acknowledged,
            "acknowledged_by": a.acknowledged_by,
            "created_at": a.created_at.isoformat(),
        }
        for a in rows
    ]


@router.post("/ops/alerts/{alert_id}/ack")
async def acknowledge_alert(
    alert_id: uuid.UUID,
    request: Request,
    actor: Principal = Depends(
        require_roles(Role.MAKER, Role.CHECKER, Role.FRAUD_ANALYST, Role.ADMIN)
    ),
) -> dict[str, Any]:
    async with request.app.state.session_factory() as session:
        alert = (
            await session.execute(select(SystemAlert).where(SystemAlert.id == alert_id))
        ).scalar_one_or_none()
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        alert.acknowledged = True
        alert.acknowledged_by = actor.subject
        await record_audit_log(
            session,
            actor_id=actor.subject,
            actor_role=actor.role.value,
            action="ALERT_ACKNOWLEDGED",
            resource_type="system_alert",
            resource_id=str(alert.id),
            payload_json={"category": alert.category, "title": alert.title},
        )
        await session.commit()
        return {"id": str(alert.id), "acknowledged": True, "acknowledged_by": actor.subject}
