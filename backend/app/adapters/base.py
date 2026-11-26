from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import redis.asyncio as redis

from app.config import Settings
from shared.iso8583.models import Iso8583Message
from shared.pki import build_client_mtls_context
from shared.schemas import (
    BankChaosConfig,
    CbsAccountOut,
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
from shared.security import (
    ROLE_SCOPES,
    Principal,
    Role,
    issue_access_token,
    sign_http_payload,
)

logger = logging.getLogger(__name__)


@dataclass
class AdapterTransportResult:
    response_message: Iso8583Message | None = None
    dropped: bool = False
    timed_out: bool = False
    latency_ms: int = 0
    signature_verified: bool = True


@dataclass
class AdapterConnectivity:
    bank_code: str
    bank_name: str
    adapter_type: str
    endpoint_url: str
    status: str  # HEALTHY | DEGRADED | OFFLINE
    latency_ms: int
    mtls_enabled: bool
    oauth2_authenticated: bool
    signature_algorithm: str
    circuit_state: str  # CLOSED | HALF_OPEN | OPEN
    cert_cn: str | None = None
    cert_fingerprint: str | None = None
    cert_valid_until: str | None = None
    chaos_preset: str = "normal"
    error: str | None = None


class BankIntegrationAdapter(Protocol):
    bank_code: str
    bank_name: str
    adapter_type: str
    supports_originator_debit: bool
    supports_reconciliation_pull: bool

    async def list_accounts(self) -> list[CbsAccountOut]: ...
    async def get_balance(self, account_number: str) -> CbsAccountOut | None: ...
    async def update_restrictions(
        self, account_number: str, req: CbsRestrictionUpdateRequest
    ) -> CbsAccountOut: ...
    async def authorize_debit(self, req: CbsDebitAuthorizeRequest) -> CbsDebitAuthorizeResponse: ...
    async def capture_debit(self, req: CbsDebitCaptureRequest) -> CbsHoldActionResponse: ...
    async def release_debit(self, req: CbsDebitReleaseRequest) -> CbsHoldActionResponse: ...
    async def execute_internal_transfer(
        self, req: CbsInternalTransferRequest
    ) -> CbsInternalTransferResponse: ...
    async def send_iso_message(
        self,
        msg: Iso8583Message,
        *,
        timeout_seconds: float,
        switch_tx_id: uuid.UUID | None = None,
    ) -> AdapterTransportResult: ...
    async def fetch_transactions(self, limit: int = 100) -> list[CbsTransactionOut]: ...
    async def inject_recon_fault(self, req: ReconFaultInjectRequest) -> CbsTransactionOut: ...
    async def get_chaos(self) -> BankChaosConfig: ...
    async def set_chaos(self, cfg: BankChaosConfig) -> BankChaosConfig: ...
    async def check_connectivity(self) -> AdapterConnectivity: ...


class HttpSignedBankAdapter:
    """Base HTTP/mTLS + OAuth 2.0 + Ed25519 signed bank integration adapter."""

    def __init__(
        self,
        *,
        bank_code: str,
        bank_name: str,
        adapter_type: str,
        base_url: str,
        settings: Settings,
        redis_client: redis.Redis | None = None,
        http_client: httpx.AsyncClient | None = None,
        supports_originator_debit: bool = True,
        supports_reconciliation_pull: bool = True,
    ) -> None:
        self.bank_code = bank_code
        self.bank_name = bank_name
        self.adapter_type = adapter_type
        self.base_url = base_url.rstrip("/")
        self.settings = settings
        self.redis_client = redis_client
        self._external_client = http_client
        self.supports_originator_debit = supports_originator_debit
        self.supports_reconciliation_pull = supports_reconciliation_pull
        self._cached_token: str | None = None
        self._token_expires_at: float = 0.0

    def _get_service_jwt(self) -> str:
        now = time.time()
        if self._cached_token and now < self._token_expires_at - 60:
            return self._cached_token
        principal = Principal(
            subject=self.settings.service_id,
            display_name="METIX Central Switch Engine",
            role=Role.SERVICE_ADAPTER,
            scopes=ROLE_SCOPES[Role.SERVICE_ADAPTER],
            client_id=self.settings.service_id,
        )
        token, exp = issue_access_token(
            principal,
            jwt_secret=self.settings.jwt_secret,
            expires_seconds=3600,
        )
        self._cached_token = token
        self._token_expires_at = now + exp
        return token

    def _build_headers(
        self,
        method: str,
        path: str,
        body_bytes: bytes,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._get_service_jwt()}",
            "Content-Type": "application/json",
        }
        sig_headers = sign_http_payload(
            method=method,
            path=path,
            body_bytes=body_bytes,
            service_id=self.settings.service_id,
            master_seed=self.settings.signing_master_seed,
        )
        headers.update(sig_headers)
        if extra:
            headers.update(extra)
        return headers

    def _build_client(self, timeout_seconds: float) -> httpx.AsyncClient:
        verify: Any = True
        if self.settings.mtls_enabled and self.base_url.startswith("https://"):
            verify = build_client_mtls_context(self.settings.certs_dir, self.settings.service_id)
        return httpx.AsyncClient(verify=verify, timeout=timeout_seconds)

    async def _record_circuit_result(self, success: bool) -> None:
        if self.redis_client is None:
            return
        fail_key = f"metix:cb:fails:{self.bank_code}"
        if success:
            await self.redis_client.delete(fail_key)
        else:
            cnt = await self.redis_client.incr(fail_key)
            await self.redis_client.expire(fail_key, 120)
            if int(cnt) >= 5:
                logger.warning("Circuit breaker OPEN threshold reached for %s", self.bank_code)

    async def _get_circuit_state(self) -> str:
        if self.redis_client is None:
            return "CLOSED"
        raw = await self.redis_client.get(f"metix:cb:fails:{self.bank_code}")
        fails = int(raw.decode() if isinstance(raw, bytes) else raw) if raw else 0
        if fails >= 5:
            return "OPEN"
        if fails >= 2:
            return "HALF_OPEN"
        return "CLOSED"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        timeout_seconds: float = 10.0,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        body_bytes = (
            json.dumps(json_body, separators=(",", ":"), default=str).encode("utf-8")
            if json_body is not None
            else b""
        )
        headers = self._build_headers(method, path, body_bytes, extra=extra_headers)
        url = f"{self.base_url}{path}"

        if self._external_client is not None:
            resp = await self._external_client.request(
                method,
                url,
                content=body_bytes if json_body is not None else None,
                headers=headers,
                timeout=timeout_seconds,
            )
            await self._record_circuit_result(resp.status_code < 500)
            return resp

        async with self._build_client(timeout_seconds) as client:
            resp = await client.request(
                method,
                url,
                content=body_bytes if json_body is not None else None,
                headers=headers,
            )
            await self._record_circuit_result(resp.status_code < 500)
            return resp
