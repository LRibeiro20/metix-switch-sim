from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select

from app.api.schemas import (
    MakerCheckerDecisionRequest,
    OAuthTokenRequest,
    OAuthTokenResponse,
)
from app.db.models import AuditLog
from app.services.audit import record_audit_log, verify_audit_chain
from app.services.maker_checker import MakerCheckerService
from shared.pki import list_pki_inventory, verify_cert_signed_by_ca
from shared.security import (
    Principal,
    Role,
    authenticate_credential,
    get_current_principal,
    issue_access_token,
    require_roles,
)

router = APIRouter(tags=["enterprise-security"])


@router.post("/oauth/token", response_model=OAuthTokenResponse)
async def oauth_issue_token(payload: OAuthTokenRequest, request: Request) -> OAuthTokenResponse:
    """OAuth 2.0 Token Endpoint (client_credentials & operator password grants)."""
    principal = authenticate_credential(
        grant_type=payload.grant_type,
        username=payload.username,
        password=payload.password,
        client_id=payload.client_id,
        client_secret=payload.client_secret,
    )
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OAuth 2.0 credentials",
        )
    token, expires_in = issue_access_token(
        principal, jwt_secret=request.app.state.settings.jwt_secret
    )
    async with request.app.state.session_factory() as session:
        await record_audit_log(
            session,
            actor_id=principal.subject,
            actor_role=principal.role.value,
            action="OAUTH2_TOKEN_ISSUED",
            resource_type="oauth_session",
            resource_id=principal.subject,
            payload_json={"grant_type": payload.grant_type, "role": principal.role.value},
        )
        await session.commit()

    return OAuthTokenResponse(
        access_token=token,
        expires_in=expires_in,
        subject=principal.subject,
        display_name=principal.display_name,
        role=principal.role.value,
        scopes=list(principal.scopes),
    )


@router.get("/api/v1/security/me")
async def get_me(principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return {
        "subject": principal.subject,
        "display_name": principal.display_name,
        "role": principal.role.value,
        "scopes": list(principal.scopes),
    }


@router.get("/api/v1/security/status")
async def get_security_status(
    request: Request,
    _: Principal = Depends(
        require_roles(Role.OPS_VIEWER, Role.MAKER, Role.CHECKER, Role.FRAUD_ANALYST, Role.ADMIN)
    ),
) -> dict[str, Any]:
    settings = request.app.state.settings
    certs = list_pki_inventory(settings.certs_dir)
    async with request.app.state.session_factory() as session:
        chain_check = await verify_audit_chain(session)

    return {
        "mtls_enabled": settings.mtls_enabled,
        "oauth2_issuer": "metix-simorede-oauth2",
        "signature_algorithm": "Ed25519 + SHA-256 + Redis Nonce Replay Guard",
        "encryption_at_rest": "AES-256-GCM (Authenticated Field Encryption for CBS PII)",
        "rbac_enforced": True,
        "maker_checker_enforced": True,
        "audit_chain": chain_check,
        "certificates": [
            {
                "service_name": c.service_name,
                "subject_cn": c.subject_cn,
                "issuer_cn": c.issuer_cn,
                "serial_hex": c.serial_hex,
                "sha256_fingerprint": c.sha256_fingerprint,
                "not_before": c.not_before.isoformat(),
                "not_after": c.not_after.isoformat(),
                "is_valid_now": c.is_valid_now,
                "ca_chain_verified": (
                    True
                    if c.service_name == "ca"
                    else verify_cert_signed_by_ca(settings.certs_dir, c.service_name)
                ),
            }
            for c in certs
        ],
    }


@router.get("/api/v1/security/audit-logs")
async def list_audit_logs(
    request: Request,
    limit: int = Query(default=60, ge=1, le=200),
    _: Principal = Depends(
        require_roles(Role.OPS_VIEWER, Role.MAKER, Role.CHECKER, Role.FRAUD_ANALYST, Role.ADMIN)
    ),
) -> list[dict[str, Any]]:
    async with request.app.state.session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLog).order_by(AuditLog.sequence_no.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": str(r.id),
            "sequence_no": int(r.sequence_no),
            "actor_id": r.actor_id,
            "actor_role": r.actor_role,
            "action": r.action,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "payload_json": r.payload_json,
            "prev_hash": r.prev_hash,
            "entry_hash": r.entry_hash,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/api/v1/security/audit-logs/verify")
async def verify_audit_logs_integrity(
    request: Request,
    _: Principal = Depends(
        require_roles(Role.OPS_VIEWER, Role.MAKER, Role.CHECKER, Role.FRAUD_ANALYST, Role.ADMIN)
    ),
) -> dict[str, Any]:
    async with request.app.state.session_factory() as session:
        return await verify_audit_chain(session)


@router.get("/api/v1/security/maker-checker")
async def list_maker_checker(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    _: Principal = Depends(
        require_roles(Role.OPS_VIEWER, Role.MAKER, Role.CHECKER, Role.FRAUD_ANALYST, Role.ADMIN)
    ),
) -> list[dict[str, Any]]:
    svc: MakerCheckerService = request.app.state.maker_checker_service
    return await svc.list_requests(limit=limit)


@router.post("/api/v1/security/maker-checker/{request_id}/decide")
async def decide_maker_checker(
    request_id: uuid.UUID,
    payload: MakerCheckerDecisionRequest,
    request: Request,
    checker: Principal = Depends(require_roles(Role.CHECKER, Role.ADMIN)),
) -> dict[str, Any]:
    svc: MakerCheckerService = request.app.state.maker_checker_service
    return await svc.decide(
        request_id,
        approve=payload.approve,
        notes=payload.notes,
        checker=checker,
    )
