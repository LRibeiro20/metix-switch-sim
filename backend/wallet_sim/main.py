from __future__ import annotations

import asyncio
import json
import secrets
import string
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import redis.asyncio as redis
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.iso8583.codec import build_0210, build_0430
from shared.iso8583.models import MTI, Iso8583Message
from shared.pki import ensure_pki_bundle, inspect_certificate
from shared.schemas import BankChaosConfig, CbsAccountOut
from shared.security import (
    DEFAULT_JWT_SECRET,
    DEFAULT_SIGNING_MASTER_SEED,
    FieldEncryptor,
    Principal,
    Role,
    require_roles,
    verify_signed_http_request,
)


class WalletSimSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WALLET_", extra="ignore")

    service_id: str = "wallet-sim"
    redis_url: str = "redis://localhost:6380/0"
    certs_dir: str = "./certs"
    mtls_enabled: bool = False
    jwt_secret: str = DEFAULT_JWT_SECRET
    signing_master_seed: str = DEFAULT_SIGNING_MASTER_SEED
    require_signed_requests: bool = True


SEED_WALLETS: dict[str, dict[str, object]] = {
    "258841234567": {
        "id": "11111111-1111-4111-8111-111111111101",
        "bank_code": "MPESA_MZ",
        "bank_name": "Vodacom M-Pesa Moçambique",
        "bic_code": "MPESMZMA",
        "account_number": "258841234567",
        "holder_name": "M-Pesa Wallet — Rosa Sitoe",
        "account_tier": "INDIVIDUAL",
        "balance_centavos": 5_000_000,
    },
    "258869876543": {
        "id": "11111111-1111-4111-8111-111111111102",
        "bank_code": "EMOLA_MZ",
        "bank_name": "Movitel e-Mola Moçambique",
        "bic_code": "EMOLMZMA",
        "account_number": "258869876543",
        "holder_name": "e-Mola Wallet — João Mabunda",
        "account_tier": "INDIVIDUAL",
        "balance_centavos": 3_500_000,
    },
}


def _gen_auth() -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))


_WALLET_CREDIT_LUA = """
local st = redis.call('HGET', KEYS[1], 'status')
if st then
    return {1, redis.call('HGET', KEYS[1], 'auth_id') or ARGV[3]}
end
redis.call('HINCRBY', KEYS[2], 'balance', tonumber(ARGV[2]))
redis.call('HINCRBY', KEYS[2], 'version', 1)
redis.call(
    'HSET', KEYS[1],
    'status', 'POSTED',
    'acct_no', ARGV[1],
    'amount', ARGV[2],
    'auth_id', ARGV[3]
)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
return {1, ARGV[3]}
"""

_WALLET_REVERSE_LUA = """
local st = redis.call('HGET', KEYS[1], 'status')
if not st or st ~= 'POSTED' then
    return {1, '00'}
end
local amt = tonumber(redis.call('HGET', KEYS[1], 'amount') or '0')
local bal = tonumber(redis.call('HGET', KEYS[2], 'balance') or '0')
if bal < amt then
    return {0, '51'}
end
redis.call('HINCRBY', KEYS[2], 'balance', -amt)
redis.call('HINCRBY', KEYS[2], 'version', 1)
redis.call('HSET', KEYS[1], 'status', 'REVERSED')
return {1, '00'}
"""


class WalletStore:
    """Redis-backed isolated wallet balance & chaos store (zero coupling to metix_switch DB)."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client
        self._credit_iso_lua = redis_client.register_script(_WALLET_CREDIT_LUA)
        self._reverse_iso_lua = redis_client.register_script(_WALLET_REVERSE_LUA)

    async def seed(self) -> None:
        for acct_no, meta in SEED_WALLETS.items():
            key = f"wallet:acct:{acct_no}"
            exists = await self.redis.exists(key)
            if not exists:
                await self.redis.hset(
                    key,
                    mapping={
                        "balance": str(meta["balance_centavos"]),
                        "version": "1",
                        "status": "ACTIVE",
                    },
                )

    async def list_wallets(self, bank_code: str | None = None) -> list[CbsAccountOut]:
        await self.seed()
        now = datetime.now(UTC)
        out: list[CbsAccountOut] = []
        for acct_no, meta in SEED_WALLETS.items():
            if bank_code and meta["bank_code"] != bank_code:
                continue
            raw = await self.redis.hgetall(f"wallet:acct:{acct_no}")
            bal = int((raw.get(b"balance") or str(meta["balance_centavos"]).encode()).decode())
            ver = int((raw.get(b"version") or b"1").decode())
            st = (raw.get(b"status") or b"ACTIVE").decode()
            holder = str(meta["holder_name"])
            out.append(
                CbsAccountOut(
                    id=uuid.UUID(str(meta["id"])),
                    bank_code=str(meta["bank_code"]),
                    bank_name=str(meta["bank_name"]),
                    bic_code=str(meta["bic_code"]),
                    account_number=acct_no,
                    holder_name=holder,
                    holder_name_masked=FieldEncryptor.mask_holder_name(holder),
                    account_tier=str(meta["account_tier"]),
                    currency="943",
                    ledger_balance_centavos=bal,
                    available_balance_centavos=bal,
                    earmarked_centavos=0,
                    status=st,
                    per_tx_limit_centavos=20_000_000,
                    daily_limit_centavos=20_000_000,
                    daily_used_centavos=0,
                    version=ver,
                    pii_encrypted_at_rest=True,
                    updated_at=now,
                )
            )
        return out

    async def get_chaos(self, bank_code: str) -> BankChaosConfig:
        raw = await self.redis.get(f"wallet:{bank_code}:chaos")
        if raw is None:
            return BankChaosConfig()
        return BankChaosConfig.model_validate(
            json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        )

    async def set_chaos(self, bank_code: str, cfg: BankChaosConfig) -> BankChaosConfig:
        await self.redis.set(f"wallet:{bank_code}:chaos", cfg.model_dump_json())
        return cfg

    async def credit(self, acct_no: str, amount: int) -> bool:
        await self.seed()
        if acct_no not in SEED_WALLETS:
            return False
        key = f"wallet:acct:{acct_no}"
        await self.redis.hincrby(key, "balance", amount)
        await self.redis.hincrby(key, "version", 1)
        return True

    async def credit_iso(
        self, acct_no: str, amount: int, stan: str, rrn: str, auth_id: str
    ) -> tuple[bool, str | None]:
        await self.seed()
        if acct_no not in SEED_WALLETS:
            return False, None
        tx_key = f"wallet:tx:{stan}:{rrn}"
        acct_key = f"wallet:acct:{acct_no}"
        res = await self._credit_iso_lua(
            keys=[tx_key, acct_key],
            args=[acct_no, amount, auth_id, 86_400],
        )
        returned_auth = res[1].decode() if isinstance(res[1], bytes) else str(res[1])
        return int(res[0]) == 1, returned_auth

    async def reverse_iso(self, acct_no: str, stan: str, rrn: str) -> str:
        await self.seed()
        if acct_no not in SEED_WALLETS:
            return "00"
        tx_key = f"wallet:tx:{stan}:{rrn}"
        acct_key = f"wallet:acct:{acct_no}"
        res = await self._reverse_iso_lua(keys=[tx_key, acct_key])
        code = res[1].decode() if isinstance(res[1], bytes) else str(res[1])
        return code

    async def debit(self, acct_no: str, amount: int) -> bool:
        await self.seed()
        if acct_no not in SEED_WALLETS:
            return False
        key = f"wallet:acct:{acct_no}"
        raw = await self.redis.hget(key, "balance")
        bal = int(raw.decode()) if raw else 0
        if bal < amount:
            return False
        await self.redis.hincrby(key, "balance", -amount)
        await self.redis.hincrby(key, "version", 1)
        return True


router = APIRouter(prefix="/wallet/v1", tags=["mobile-wallet-simulator"])


async def _verify_sig(request: Request) -> dict[str, str]:
    settings: WalletSimSettings = request.app.state.settings
    if not settings.require_signed_requests:
        return {"key_id": "unsigned-dev", "algorithm": "NONE"}
    return await verify_signed_http_request(
        request,
        redis_client=request.app.state.redis,
        allowed_key_ids={"switch-api"},
        master_seed=settings.signing_master_seed,
    )


@router.get("/health")
async def wallet_health(request: Request) -> dict[str, object]:
    settings: WalletSimSettings = request.app.state.settings
    pong = await request.app.state.redis.ping()
    cert_meta = inspect_certificate(settings.certs_dir, settings.service_id)
    return {
        "status": "ok",
        "service_id": settings.service_id,
        "providers": ["MPESA_MZ", "EMOLA_MZ"],
        "redis": "connected" if pong else "error",
        "mtls_enabled": settings.mtls_enabled,
        "cert_cn": cert_meta.subject_cn if cert_meta else settings.service_id,
        "cert_fingerprint": cert_meta.sha256_fingerprint if cert_meta else None,
        "cert_valid_until": cert_meta.not_after.isoformat() if cert_meta else None,
    }


@router.get("/accounts", response_model=list[CbsAccountOut])
async def list_wallet_accounts(
    request: Request,
    bank_code: str | None = None,
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
    store: WalletStore = request.app.state.wallet_store
    return await store.list_wallets(bank_code=bank_code)


@router.get("/chaos/{bank_code}", response_model=BankChaosConfig)
async def get_wallet_chaos(
    bank_code: str,
    request: Request,
    _: Principal = Depends(require_roles(Role.SERVICE_ADAPTER, Role.ADMIN, Role.OPS_VIEWER)),
) -> BankChaosConfig:
    store: WalletStore = request.app.state.wallet_store
    return await store.get_chaos(bank_code)


@router.post("/chaos/{bank_code}", response_model=BankChaosConfig)
async def set_wallet_chaos(
    bank_code: str,
    payload: BankChaosConfig,
    request: Request,
    _: Principal = Depends(require_roles(Role.SERVICE_ADAPTER, Role.ADMIN)),
    _sig: dict[str, str] = Depends(_verify_sig),
) -> BankChaosConfig:
    store: WalletStore = request.app.state.wallet_store
    return await store.set_chaos(bank_code, payload)


@router.post("/iso8583")
async def receive_wallet_iso8583(
    msg: Iso8583Message,
    request: Request,
    _: Principal = Depends(require_roles(Role.SERVICE_ADAPTER, Role.ADMIN)),
    _sig: dict[str, str] = Depends(_verify_sig),
) -> JSONResponse:
    store: WalletStore = request.app.state.wallet_store
    ben_bank, ben_acct = msg.de103.split(":", 1)
    chaos = await store.get_chaos(ben_bank)
    if chaos.offline:
        return JSONResponse(status_code=503, content={"dropped": True, "reason": "Wallet offline"})

    if msg.mti == MTI.FINANCIAL_REQUEST:
        if chaos.drop_0210:
            await asyncio.sleep(min(chaos.latency_ms, 250) / 1000.0)
            return JSONResponse(
                status_code=202,
                content={"dropped": True, "beneficiary": ben_bank, "reason": "Chaos drop_0210"},
            )
        if chaos.latency_ms > 0:
            await asyncio.sleep(chaos.latency_ms / 1000.0)
        if chaos.force_de39 and chaos.force_de39 != "00":
            resp = build_0210(msg, response_code=chaos.force_de39)
            return JSONResponse(
                status_code=200,
                content={"dropped": False, "iso_message": resp.model_dump(mode="json")},
            )
        ok, auth_id = await store.credit_iso(ben_acct, msg.de4, msg.de11, msg.de37, _gen_auth())
        if not ok:
            resp = build_0210(msg, response_code="14")
            return JSONResponse(
                status_code=200,
                content={"dropped": False, "iso_message": resp.model_dump(mode="json")},
            )
        resp = build_0210(msg, response_code="00", auth_id=auth_id)
        if chaos.commit_then_drop_0210:
            return JSONResponse(
                status_code=202,
                content={
                    "dropped": True,
                    "beneficiary": ben_bank,
                    "reason": (
                        "Chaos commit_then_drop_0210 active — wallet credit committed, "
                        "0210 ACK dropped on wire"
                    ),
                },
            )
        return JSONResponse(
            status_code=200,
            content={"dropped": False, "iso_message": resp.model_dump(mode="json")},
        )

    if msg.mti == MTI.REVERSAL_ADVICE:
        if chaos.fail_0430:
            return JSONResponse(
                status_code=202,
                content={"dropped": True, "beneficiary": ben_bank, "reason": "Chaos fail_0430"},
            )
        de39 = await store.reverse_iso(ben_acct, msg.de11, msg.de37)
        resp = build_0430(msg, response_code=de39)
        return JSONResponse(
            status_code=200,
            content={"dropped": False, "iso_message": resp.model_dump(mode="json")},
        )

    raise HTTPException(status_code=400, detail=f"Unsupported MTI: {msg.mti.value}")


def create_wallet_app(settings: WalletSimSettings | None = None) -> FastAPI:
    cfg = settings or WalletSimSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ensure_pki_bundle(cfg.certs_dir)
        redis_client = redis.from_url(cfg.redis_url, decode_responses=False)
        store = WalletStore(redis_client)
        await store.seed()
        app.state.settings = cfg
        app.state.redis = redis_client
        app.state.wallet_store = store
        try:
            yield
        finally:
            await redis_client.aclose()

    app = FastAPI(title="Mobile Wallet Adapter Simulator (MPESA_MZ / EMOLA_MZ)", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_wallet_app()
