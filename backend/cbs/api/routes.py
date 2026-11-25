from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from cbs.services.core_engine import CbsCoreEngine
from shared.iso8583.models import Iso8583Message
from shared.pki import inspect_certificate
from shared.schemas import (
    BankChaosConfig,
    CbsAccountOut,
    CbsDebitAuthorizeRequest,
    CbsDebitAuthorizeResponse,
    CbsDebitCaptureRequest,
    CbsDebitReleaseRequest,
    CbsHoldActionResponse,
    CbsRestrictionUpdateRequest,
    CbsTransactionOut,
    ReconFaultInjectRequest,
)
from shared.security import (
    Principal,
    Role,
    require_roles,
    verify_signed_http_request,
)

router = APIRouter(prefix="/cbs/v1", tags=["core-banking-simulator"])

ALLOWED_SIGNERS = {"switch-api", "bci-cbs", "stdb-cbs"}


async def _verify_adapter_signature(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    if not settings.require_signed_requests:
        return {"key_id": "unsigned-dev", "algorithm": "NONE"}
    return await verify_signed_http_request(
        request,
        redis_client=request.app.state.redis,
        allowed_key_ids=ALLOWED_SIGNERS,
        master_seed=settings.signing_master_seed,
    )


def _engine(request: Request) -> CbsCoreEngine:
    return request.app.state.cbs_engine


@router.get("/health")
async def cbs_health(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    async with request.app.state.session_factory() as session:
        await session.execute(text("SELECT 1"))
    pong = await request.app.state.redis.ping()
    cert_meta = inspect_certificate(settings.certs_dir, settings.service_id)
    chaos = await _engine(request).get_chaos()
    return {
        "status": "offline" if chaos.offline else "ok",
        "bank_code": settings.bank_code,
        "bank_name": settings.bank_name,
        "bic_code": settings.bic_code,
        "service_id": settings.service_id,
        "postgres": "connected",
        "redis": "connected" if pong else "error",
        "mtls_enabled": settings.mtls_enabled,
        "cert_cn": cert_meta.subject_cn if cert_meta else settings.service_id,
        "cert_fingerprint": cert_meta.sha256_fingerprint if cert_meta else None,
        "cert_valid_until": cert_meta.not_after.isoformat() if cert_meta else None,
        "chaos_preset": chaos.preset,
    }


@router.get("/accounts", response_model=list[CbsAccountOut])
async def list_cbs_accounts(
    request: Request,
    include_gl: bool = Query(default=False),
    _: Principal = Depends(
        require_roles(
            Role.SERVICE_ADAPTER,
            Role.ADMIN,
            Role.OPS_VIEWER,
            Role.MAKER,
            Role.CHECKER,
            Role.FRAUD_ANALYST,
        )
    ),
) -> list[CbsAccountOut]:
    return await _engine(request).list_accounts(include_gl=include_gl)


@router.get("/accounts/{account_number}/balance", response_model=CbsAccountOut)
async def get_cbs_balance(
    account_number: str,
    request: Request,
    _: Principal = Depends(
        require_roles(
            Role.SERVICE_ADAPTER,
            Role.ADMIN,
            Role.OPS_VIEWER,
            Role.MAKER,
            Role.CHECKER,
            Role.FRAUD_ANALYST,
        )
    ),
) -> CbsAccountOut:
    acct = await _engine(request).get_account_balance(account_number)
    if acct is None:
        raise HTTPException(status_code=404, detail=f"Account {account_number} not found")
    return acct


@router.post("/accounts/{account_number}/restrictions", response_model=CbsAccountOut)
async def update_cbs_restrictions(
    account_number: str,
    payload: CbsRestrictionUpdateRequest,
    request: Request,
    _: Principal = Depends(require_roles(Role.SERVICE_ADAPTER, Role.ADMIN, Role.CHECKER)),
    _sig: dict[str, str] = Depends(_verify_adapter_signature),
) -> CbsAccountOut:
    try:
        return await _engine(request).update_restrictions(account_number, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/debits/authorize", response_model=CbsDebitAuthorizeResponse)
async def authorize_cbs_debit(
    payload: CbsDebitAuthorizeRequest,
    request: Request,
    _: Principal = Depends(require_roles(Role.SERVICE_ADAPTER, Role.ADMIN)),
    _sig: dict[str, str] = Depends(_verify_adapter_signature),
) -> CbsDebitAuthorizeResponse:
    return await _engine(request).authorize_debit(payload, signature_verified=True)


@router.post("/debits/capture", response_model=CbsHoldActionResponse)
async def capture_cbs_debit(
    payload: CbsDebitCaptureRequest,
    request: Request,
    _: Principal = Depends(require_roles(Role.SERVICE_ADAPTER, Role.ADMIN)),
    _sig: dict[str, str] = Depends(_verify_adapter_signature),
) -> CbsHoldActionResponse:
    try:
        return await _engine(request).capture_debit(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/debits/release", response_model=CbsHoldActionResponse)
async def release_cbs_debit(
    payload: CbsDebitReleaseRequest,
    request: Request,
    _: Principal = Depends(require_roles(Role.SERVICE_ADAPTER, Role.ADMIN)),
    _sig: dict[str, str] = Depends(_verify_adapter_signature),
) -> CbsHoldActionResponse:
    try:
        return await _engine(request).release_debit(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/iso8583")
async def receive_cbs_iso8583(
    msg: Iso8583Message,
    request: Request,
    _: Principal = Depends(require_roles(Role.SERVICE_ADAPTER, Role.ADMIN)),
    _sig: dict[str, str] = Depends(_verify_adapter_signature),
) -> JSONResponse:
    raw_tx_id = request.headers.get("x-switch-tx-id")
    switch_tx_id = uuid.UUID(raw_tx_id) if raw_tx_id else None
    code, body = await _engine(request).process_iso8583(
        msg, switch_tx_id=switch_tx_id, signature_verified=True
    )
    return JSONResponse(status_code=code, content=body)


@router.get("/transactions", response_model=list[CbsTransactionOut])
async def list_cbs_transactions(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    _: Principal = Depends(
        require_roles(
            Role.SERVICE_ADAPTER,
            Role.ADMIN,
            Role.OPS_VIEWER,
            Role.MAKER,
            Role.CHECKER,
            Role.FRAUD_ANALYST,
        )
    ),
) -> list[CbsTransactionOut]:
    return await _engine(request).list_transactions(limit=limit)


@router.post("/recon/inject-fault", response_model=CbsTransactionOut)
async def inject_cbs_recon_fault(
    payload: ReconFaultInjectRequest,
    request: Request,
    _: Principal = Depends(require_roles(Role.SERVICE_ADAPTER, Role.ADMIN, Role.MAKER)),
    _sig: dict[str, str] = Depends(_verify_adapter_signature),
) -> CbsTransactionOut:
    try:
        return await _engine(request).inject_recon_fault(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/chaos", response_model=BankChaosConfig)
async def get_cbs_chaos(
    request: Request,
    _: Principal = Depends(
        require_roles(
            Role.SERVICE_ADAPTER,
            Role.ADMIN,
            Role.OPS_VIEWER,
            Role.MAKER,
            Role.CHECKER,
            Role.FRAUD_ANALYST,
        )
    ),
) -> BankChaosConfig:
    return await _engine(request).get_chaos()


@router.post("/chaos", response_model=BankChaosConfig)
async def set_cbs_chaos(
    payload: BankChaosConfig,
    request: Request,
    _: Principal = Depends(require_roles(Role.SERVICE_ADAPTER, Role.ADMIN)),
    _sig: dict[str, str] = Depends(_verify_adapter_signature),
) -> BankChaosConfig:
    return await _engine(request).set_chaos(payload)
