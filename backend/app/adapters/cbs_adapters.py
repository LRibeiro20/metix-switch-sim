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
    CbsInternalTransferRequest,
    CbsInternalTransferResponse,
    CbsRestrictionUpdateRequest,
    CbsTransactionOut,
    ReconFaultInjectRequest,
)

logger = logging.getLogger(__name__)


class CoreBankingHttpAdapter(HttpSignedBankAdapter):
    """Integration adapter communicating with an independent Core Banking Simulator."""

    async def list_accounts(self) -> list[CbsAccountOut]:
        resp = await self._request("GET", "/cbs/v1/accounts", timeout_seconds=5.0)
        resp.raise_for_status()
        return [CbsAccountOut.model_validate(item) for item in resp.json()]

    async def get_balance(self, account_number: str) -> CbsAccountOut | None:
        resp = await self._request(
            "GET", f"/cbs/v1/accounts/{account_number}/balance", timeout_seconds=5.0
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return CbsAccountOut.model_validate(resp.json())

    async def update_restrictions(
        self, account_number: str, req: CbsRestrictionUpdateRequest
    ) -> CbsAccountOut:
        resp = await self._request(
            "POST",
            f"/cbs/v1/accounts/{account_number}/restrictions",
            json_body=req.model_dump(mode="json", exclude_none=True),
            timeout_seconds=5.0,
        )
        resp.raise_for_status()
        return CbsAccountOut.model_validate(resp.json())

    async def authorize_debit(self, req: CbsDebitAuthorizeRequest) -> CbsDebitAuthorizeResponse:
        try:
            resp = await self._request(
                "POST",
                "/cbs/v1/debits/authorize",
                json_body=req.model_dump(mode="json"),
                timeout_seconds=8.0,
            )
        except httpx.HTTPError as exc:
            logger.error("Originator CBS %s debit authorize error: %s", self.bank_code, exc)
            await self._record_circuit_result(False)
            return CbsDebitAuthorizeResponse(
                approved=False,
                response_code="91",
                reason=f"Originator CBS {self.bank_code} unreachable: {exc}",
            )

        if resp.status_code != 200:
            return CbsDebitAuthorizeResponse(
                approved=False,
                response_code="91",
                reason=f"Originator CBS {self.bank_code} HTTP {resp.status_code}",
            )
        return CbsDebitAuthorizeResponse.model_validate(resp.json())

    async def capture_debit(self, req: CbsDebitCaptureRequest) -> CbsHoldActionResponse:
        resp = await self._request(
            "POST",
            "/cbs/v1/debits/capture",
            json_body=req.model_dump(mode="json"),
            timeout_seconds=8.0,
        )
        resp.raise_for_status()
        return CbsHoldActionResponse.model_validate(resp.json())

    async def release_debit(self, req: CbsDebitReleaseRequest) -> CbsHoldActionResponse:
        resp = await self._request(
            "POST",
            "/cbs/v1/debits/release",
            json_body=req.model_dump(mode="json"),
            timeout_seconds=8.0,
        )
        resp.raise_for_status()
        return CbsHoldActionResponse.model_validate(resp.json())

    async def execute_internal_transfer(
        self, req: CbsInternalTransferRequest
    ) -> CbsInternalTransferResponse:
        try:
            resp = await self._request(
                "POST",
                "/cbs/v1/transfers/internal",
                json_body=req.model_dump(mode="json"),
                timeout_seconds=8.0,
            )
        except httpx.HTTPError as exc:
            logger.error("CBS %s internal transfer transport error: %s", self.bank_code, exc)
            await self._record_circuit_result(False)
            return CbsInternalTransferResponse(
                idempotent_replay=False,
                approved=False,
                bank_code=self.bank_code,
                response_code="91",
                reason=f"CBS {self.bank_code} unreachable for on-us transfer: {exc}",
                originator_account_number=req.originator_account_number,
                beneficiary_account_number=req.beneficiary_account_number,
                amount_centavos=req.amount_centavos,
            )

        if resp.status_code == 400:
            detail = resp.json().get("detail", "Invalid on-us transfer request")
            raise ValueError(str(detail))
        resp.raise_for_status()
        return CbsInternalTransferResponse.model_validate(resp.json())

    async def send_iso_message(
        self,
        msg: Iso8583Message,
        *,
        timeout_seconds: float,
        switch_tx_id: uuid.UUID | None = None,
    ) -> AdapterTransportResult:
        started = time.perf_counter()
        extra = {"X-Switch-Tx-Id": str(switch_tx_id)} if switch_tx_id else None
        try:
            resp = await self._request(
                "POST",
                "/cbs/v1/iso8583",
                json_body=msg.model_dump(mode="json"),
                timeout_seconds=timeout_seconds,
                extra_headers=extra,
            )
        except httpx.TimeoutException:
            elapsed = int((time.perf_counter() - started) * 1000)
            await self._record_circuit_result(False)
            return AdapterTransportResult(timed_out=True, latency_ms=elapsed)
        except httpx.HTTPError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.error("CBS %s ISO transport error: %s", self.bank_code, exc)
            await self._record_circuit_result(False)
            return AdapterTransportResult(dropped=True, latency_ms=elapsed)

        elapsed = int((time.perf_counter() - started) * 1000)
        if resp.status_code == 202 or resp.status_code != 200:
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
        resp = await self._request(
            "GET", f"/cbs/v1/transactions?limit={limit}", timeout_seconds=5.0
        )
        resp.raise_for_status()
        return [CbsTransactionOut.model_validate(item) for item in resp.json()]

    async def inject_recon_fault(self, req: ReconFaultInjectRequest) -> CbsTransactionOut:
        resp = await self._request(
            "POST",
            "/cbs/v1/recon/inject-fault",
            json_body=req.model_dump(mode="json"),
            timeout_seconds=5.0,
        )
        resp.raise_for_status()
        return CbsTransactionOut.model_validate(resp.json())

    async def get_chaos(self) -> BankChaosConfig:
        resp = await self._request("GET", "/cbs/v1/chaos", timeout_seconds=4.0)
        resp.raise_for_status()
        return BankChaosConfig.model_validate(resp.json())

    async def set_chaos(self, cfg: BankChaosConfig) -> BankChaosConfig:
        resp = await self._request(
            "POST",
            "/cbs/v1/chaos",
            json_body=cfg.model_dump(mode="json"),
            timeout_seconds=4.0,
        )
        resp.raise_for_status()
        return BankChaosConfig.model_validate(resp.json())

    async def check_connectivity(self) -> AdapterConnectivity:
        started = time.perf_counter()
        circuit = await self._get_circuit_state()
        try:
            resp = await self._request("GET", "/cbs/v1/health", timeout_seconds=3.0)
            elapsed = int((time.perf_counter() - started) * 1000)
            data = resp.json()
            is_ok = resp.status_code == 200 and data.get("status") == "ok"
            chaos_preset = str(data.get("chaos_preset", "normal"))
            status_label = (
                "OFFLINE"
                if data.get("status") == "offline"
                else "DEGRADED"
                if chaos_preset != "normal" or circuit != "CLOSED"
                else "HEALTHY"
                if is_ok
                else "DEGRADED"
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
                chaos_preset=chaos_preset,
            )
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return AdapterConnectivity(
                bank_code=self.bank_code,
                bank_name=self.bank_name,
                adapter_type=self.adapter_type,
                endpoint_url=self.base_url,
                status="OFFLINE",
                latency_ms=elapsed,
                mtls_enabled=self.settings.mtls_enabled,
                oauth2_authenticated=False,
                signature_algorithm="Ed25519",
                circuit_state="OPEN",
                error=str(exc),
            )


class BciCoreBankingAdapter(CoreBankingHttpAdapter):
    """Integration adapter for Banco Comercial e de Investimentos (BCI_MZ)."""

    def __init__(
        self,
        settings: Settings,
        *,
        redis_client: redis.Redis | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            bank_code="BCI_MZ",
            bank_name="Banco Comercial e de Investimentos",
            adapter_type="CORE_BANKING_CBS",
            base_url=settings.bci_cbs_base_url,
            settings=settings,
            redis_client=redis_client,
            http_client=http_client,
            supports_originator_debit=True,
            supports_reconciliation_pull=True,
        )


class StandardBankCoreBankingAdapter(CoreBankingHttpAdapter):
    """Integration adapter for Standard Bank Moçambique (STDB_MZ)."""

    def __init__(
        self,
        settings: Settings,
        *,
        redis_client: redis.Redis | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            bank_code="STDB_MZ",
            bank_name="Standard Bank Moçambique",
            adapter_type="CORE_BANKING_CBS",
            base_url=settings.stdb_cbs_base_url,
            settings=settings,
            redis_client=redis_client,
            http_client=http_client,
            supports_originator_debit=True,
            supports_reconciliation_pull=True,
        )
