from __future__ import annotations

import logging
import time
import uuid

import httpx
import redis.asyncio as redis

from app.adapters.base import (
    AdapterConnectivity,
    AdapterTransportResult,
    HttpSignedBankAdapter,
)
from app.config import Settings
from shared.iso8583.models import Iso8583Message
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

logger = logging.getLogger(__name__)


class MobileWalletAdapter(HttpSignedBankAdapter):
    """Optional external mobile wallet adapter for MPESA_MZ or EMOLA_MZ."""

    def __init__(
        self,
        *,
        bank_code: str,
        bank_name: str,
        settings: Settings,
        redis_client: redis.Redis | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            bank_code=bank_code,
            bank_name=bank_name,
            adapter_type="MOBILE_WALLET_PSP",
            base_url=settings.wallet_sim_base_url,
            settings=settings,
            redis_client=redis_client,
            http_client=http_client,
            supports_originator_debit=False,
            supports_reconciliation_pull=False,
        )

    async def list_accounts(self) -> list[CbsAccountOut]:
        resp = await self._request(
            "GET", f"/wallet/v1/accounts?bank_code={self.bank_code}", timeout_seconds=5.0
        )
        resp.raise_for_status()
        return [CbsAccountOut.model_validate(item) for item in resp.json()]

    async def get_balance(self, account_number: str) -> CbsAccountOut | None:
        for acct in await self.list_accounts():
            if acct.account_number == account_number:
                return acct
        return None

    async def update_restrictions(
        self, account_number: str, req: CbsRestrictionUpdateRequest
    ) -> CbsAccountOut:
        raise NotImplementedError("Mobile wallet restrictions are managed by wallet operator")

    async def authorize_debit(self, req: CbsDebitAuthorizeRequest) -> CbsDebitAuthorizeResponse:
        return CbsDebitAuthorizeResponse(
            approved=True,
            response_code="00",
            hold_ref=f"WALLET-{self.bank_code}-{req.stan}",
            cbs_ref=f"WLT-{req.stan}",
        )

    async def capture_debit(self, req: CbsDebitCaptureRequest) -> CbsHoldActionResponse:
        return CbsHoldActionResponse(
            ok=True,
            hold_ref=req.hold_ref,
            status="CAPTURED",
            available_balance_centavos=0,
            ledger_balance_centavos=0,
        )

    async def release_debit(self, req: CbsDebitReleaseRequest) -> CbsHoldActionResponse:
        return CbsHoldActionResponse(
            ok=True,
            hold_ref=req.hold_ref,
            status="RELEASED",
            available_balance_centavos=0,
            ledger_balance_centavos=0,
        )

    async def send_iso_message(
        self,
        msg: Iso8583Message,
        *,
        timeout_seconds: float,
        switch_tx_id: uuid.UUID | None = None,
    ) -> AdapterTransportResult:
        started = time.perf_counter()
        try:
            resp = await self._request(
                "POST",
                "/wallet/v1/iso8583",
                json_body=msg.model_dump(mode="json"),
                timeout_seconds=timeout_seconds,
            )
        except httpx.TimeoutException:
            return AdapterTransportResult(
                timed_out=True, latency_ms=int((time.perf_counter() - started) * 1000)
            )
        except httpx.HTTPError as exc:
            logger.error("Wallet adapter %s transport error: %s", self.bank_code, exc)
            return AdapterTransportResult(
                dropped=True, latency_ms=int((time.perf_counter() - started) * 1000)
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            return AdapterTransportResult(dropped=True, latency_ms=elapsed)
        body = resp.json()
        if body.get("dropped"):
            return AdapterTransportResult(dropped=True, latency_ms=elapsed)
        return AdapterTransportResult(
            response_message=Iso8583Message.model_validate(body["iso_message"]),
            latency_ms=elapsed,
            signature_verified=True,
        )

    async def fetch_transactions(self, limit: int = 100) -> list[CbsTransactionOut]:
        return []

    async def inject_recon_fault(self, req: ReconFaultInjectRequest) -> CbsTransactionOut:
        raise NotImplementedError("Recon fault injection applies to Core Banking Simulators")

    async def get_chaos(self) -> BankChaosConfig:
        resp = await self._request("GET", f"/wallet/v1/chaos/{self.bank_code}", timeout_seconds=4.0)
        resp.raise_for_status()
        return BankChaosConfig.model_validate(resp.json())

    async def set_chaos(self, cfg: BankChaosConfig) -> BankChaosConfig:
        resp = await self._request(
            "POST",
            f"/wallet/v1/chaos/{self.bank_code}",
            json_body=cfg.model_dump(mode="json"),
            timeout_seconds=4.0,
        )
        resp.raise_for_status()
        return BankChaosConfig.model_validate(resp.json())

    async def check_connectivity(self) -> AdapterConnectivity:
        started = time.perf_counter()
        circuit = await self._get_circuit_state()
        try:
            resp = await self._request("GET", "/wallet/v1/health", timeout_seconds=3.0)
            elapsed = int((time.perf_counter() - started) * 1000)
            data = resp.json()
            chaos = await self.get_chaos()
            status_label = (
                "OFFLINE"
                if chaos.offline
                else "DEGRADED"
                if chaos.preset != "normal" or circuit != "CLOSED"
                else "HEALTHY"
            )
            return AdapterConnectivity(
                bank_code=self.bank_code,
                bank_name=self.bank_name,
                adapter_type=self.adapter_type,
                endpoint_url=self.base_url,
                status=status_label,
                latency_ms=elapsed,
                mtls_enabled=bool(data.get("mtls_enabled", self.settings.mtls_enabled)),
                oauth2_authenticated=True,
                signature_algorithm="Ed25519",
                circuit_state=circuit,
                cert_cn=data.get("cert_cn"),
                cert_fingerprint=data.get("cert_fingerprint"),
                cert_valid_until=data.get("cert_valid_until"),
                chaos_preset=chaos.preset,
            )
        except Exception as exc:
            return AdapterConnectivity(
                bank_code=self.bank_code,
                bank_name=self.bank_name,
                adapter_type=self.adapter_type,
                endpoint_url=self.base_url,
                status="OFFLINE",
                latency_ms=int((time.perf_counter() - started) * 1000),
                mtls_enabled=self.settings.mtls_enabled,
                oauth2_authenticated=False,
                signature_algorithm="Ed25519",
                circuit_state="OPEN",
                error=str(exc),
            )
