from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.registry import AdapterRegistry
from app.db.models import (
    Account,
    JournalEntryType,
    MakerCheckerRequest,
    ReconciliationException,
    SwitchTransaction,
)
from app.db.seed import METIX_SUSPENSE_ACCOUNT_NUMBER
from app.services.audit import record_audit_log
from app.services.ledger import post_balanced_journal
from app.services.settlement import SettlementService
from shared.schemas import CbsDebitReleaseRequest, CbsRestrictionUpdateRequest
from shared.security import Principal, Role


class MakerCheckerService:
    """Enforces strict 4-eyes authorization (maker_id != checker_id) on sensitive ops actions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        adapter_registry: AdapterRegistry,
        settlement_service: SettlementService,
    ) -> None:
        self.session_factory = session_factory
        self.adapters = adapter_registry
        self.settlement = settlement_service

    @staticmethod
    def serialize(req: MakerCheckerRequest) -> dict[str, Any]:
        return {
            "id": str(req.id),
            "action_type": req.action_type,
            "resource_type": req.resource_type,
            "resource_id": req.resource_id,
            "payload_json": req.payload_json,
            "reason": req.reason,
            "status": req.status,
            "maker_id": req.maker_id,
            "maker_role": req.maker_role,
            "checker_id": req.checker_id,
            "checker_role": req.checker_role,
            "decision_notes": req.decision_notes,
            "created_at": req.created_at.isoformat(),
            "decided_at": req.decided_at.isoformat() if req.decided_at else None,
        }

    async def propose(
        self,
        *,
        action_type: str,
        resource_type: str,
        resource_id: str,
        payload_json: dict[str, Any],
        reason: str,
        maker: Principal,
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            req = MakerCheckerRequest(
                action_type=action_type,
                resource_type=resource_type,
                resource_id=resource_id,
                payload_json=payload_json,
                reason=reason,
                status="PENDING",
                maker_id=maker.subject,
                maker_role=maker.role.value,
            )
            session.add(req)
            await session.flush()

            if action_type == "RECON_ADJUSTMENT":
                exc = (
                    await session.execute(
                        select(ReconciliationException).where(
                            ReconciliationException.id == uuid.UUID(resource_id)
                        )
                    )
                ).scalar_one_or_none()
                if exc is not None and exc.status == "OPEN":
                    exc.status = "PENDING_ADJUSTMENT"

            await record_audit_log(
                session,
                actor_id=maker.subject,
                actor_role=maker.role.value,
                action=f"MAKER_PROPOSE_{action_type}",
                resource_type=resource_type,
                resource_id=resource_id,
                payload_json={"request_id": str(req.id), "reason": reason, **payload_json},
            )
            await session.commit()
            await session.refresh(req)
            return self.serialize(req)

    async def list_requests(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(MakerCheckerRequest)
                        .order_by(MakerCheckerRequest.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [self.serialize(r) for r in rows]

    async def decide(
        self,
        request_id: uuid.UUID,
        *,
        approve: bool,
        notes: str,
        checker: Principal,
    ) -> dict[str, Any]:
        if checker.role not in (Role.CHECKER, Role.ADMIN):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only CHECKER or ADMIN role can approve or reject Maker-Checker requests",
            )

        async with self.session_factory() as session:
            req = (
                await session.execute(
                    select(MakerCheckerRequest)
                    .where(MakerCheckerRequest.id == request_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if req is None:
                raise HTTPException(status_code=404, detail="Maker-Checker request not found")
            if req.status != "PENDING":
                raise HTTPException(
                    status_code=409,
                    detail=f"Maker-Checker request already finalized ({req.status})",
                )
            if req.maker_id == checker.subject:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Maker-Checker 4-eyes violation: checker cannot approve or reject "
                        "their own proposed request"
                    ),
                )

            req.checker_id = checker.subject
            req.checker_role = checker.role.value
            req.decision_notes = notes
            req.decided_at = datetime.now(UTC)

            if not approve:
                req.status = "REJECTED"
                if req.action_type == "RECON_ADJUSTMENT":
                    exc = (
                        await session.execute(
                            select(ReconciliationException).where(
                                ReconciliationException.id == uuid.UUID(req.resource_id)
                            )
                        )
                    ).scalar_one_or_none()
                    if exc is not None and exc.status == "PENDING_ADJUSTMENT":
                        exc.status = "OPEN"
                await record_audit_log(
                    session,
                    actor_id=checker.subject,
                    actor_role=checker.role.value,
                    action=f"CHECKER_REJECT_{req.action_type}",
                    resource_type=req.resource_type,
                    resource_id=req.resource_id,
                    payload_json={"request_id": str(req.id), "notes": notes},
                )
                await session.commit()
                return self.serialize(req)

            # Approved: execute domain action cleanly
            req.status = "APPROVED"
            action_result: dict[str, Any] = {}

            if req.action_type == "RECON_ADJUSTMENT":
                action_result = await self._execute_recon_adjustment(session, req, checker, notes)
            elif req.action_type == "ACCOUNT_RESTRICTION":
                bank_code = str(req.payload_json["bank_code"])
                account_number = str(req.payload_json["account_number"])
                update_req = CbsRestrictionUpdateRequest.model_validate(
                    {
                        k: v
                        for k, v in req.payload_json.items()
                        if k
                        in {"status", "per_tx_limit_centavos", "daily_limit_centavos", "reason"}
                    }
                )
                updated_acct = await self.adapters.get(bank_code).update_restrictions(
                    account_number, update_req
                )
                action_result = updated_acct.model_dump(mode="json")
            elif req.action_type == "LIQUIDITY_TOPUP":
                action_result = await self.settlement.topup_liquidity(
                    institution_code=str(req.payload_json["institution_code"]),
                    amount_centavos=int(req.payload_json["amount_centavos"]),
                    actor=checker,
                    reason=req.reason,
                )

            await record_audit_log(
                session,
                actor_id=checker.subject,
                actor_role=checker.role.value,
                action=f"CHECKER_APPROVE_{req.action_type}",
                resource_type=req.resource_type,
                resource_id=req.resource_id,
                payload_json={"request_id": str(req.id), "notes": notes, "result": action_result},
            )
            await session.commit()
            return {**self.serialize(req), "execution_result": action_result}

    async def _execute_recon_adjustment(
        self,
        session: AsyncSession,
        req: MakerCheckerRequest,
        checker: Principal,
        notes: str,
    ) -> dict[str, Any]:
        exc = (
            await session.execute(
                select(ReconciliationException)
                .where(ReconciliationException.id == uuid.UUID(req.resource_id))
                .with_for_update()
            )
        ).scalar_one()
        adj_mode = str(req.payload_json.get("adjustment_mode", "RELEASE_SUSPENSE_TO_ORIGINATOR"))
        journal_posted = False

        if exc.switch_tx_id is not None and adj_mode in (
            "RELEASE_SUSPENSE_TO_ORIGINATOR",
            "FORCE_SETTLE_TO_BENEFICIARY",
        ):
            stx = (
                await session.execute(
                    select(SwitchTransaction)
                    .where(SwitchTransaction.id == exc.switch_tx_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            suspense = (
                await session.execute(
                    select(Account).where(Account.account_number == METIX_SUSPENSE_ACCOUNT_NUMBER)
                )
            ).scalar_one()
            if stx is not None and suspense.balance_centavos >= stx.amount_centavos:
                credit_target = (
                    stx.originator_account_id
                    if adj_mode == "RELEASE_SUSPENSE_TO_ORIGINATOR"
                    else stx.beneficiary_account_id
                )
                await post_balanced_journal(
                    session,
                    transaction_id=stx.id,
                    entry_type=JournalEntryType.RECON_ADJUSTMENT,
                    description=(
                        f"Maker-Checker recon adjustment ({adj_mode}) approved by "
                        f"{checker.subject} for STAN {stx.stan}"
                    ),
                    debit_account_id=suspense.id,
                    credit_account_id=credit_target,
                    amount_centavos=stx.amount_centavos,
                )
                journal_posted = True
                if (
                    adj_mode == "RELEASE_SUSPENSE_TO_ORIGINATOR"
                    and stx.originator_hold_ref
                    and self.adapters.get(stx.originator_code).supports_originator_debit
                ):
                    with contextlib.suppress(Exception):
                        await self.adapters.get(stx.originator_code).release_debit(
                            CbsDebitReleaseRequest(
                                switch_tx_id=stx.id,
                                hold_ref=stx.originator_hold_ref,
                                stan=stx.stan,
                                rrn=stx.rrn,
                                reason=f"Recon adjustment approved by {checker.subject}",
                            )
                        )

        exc.status = "RESOLVED"
        exc.resolution_notes = f"[{adj_mode}] Approved by {checker.subject}: {notes}"
        exc.resolved_at = datetime.now(UTC)
        return {"exception_id": str(exc.id), "mode": adj_mode, "journal_posted": journal_posted}
