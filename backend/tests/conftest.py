from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest_asyncio
import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.cbs_adapters import BciCoreBankingAdapter, StandardBankCoreBankingAdapter
from app.adapters.registry import AdapterRegistry
from app.adapters.wallet_adapter import MobileWalletAdapter
from app.config import Settings
from app.db.models import Base
from app.db.seed import seed_database
from app.main import create_app
from app.services.maker_checker import MakerCheckerService
from app.services.reconciliation import ReconciliationService
from app.services.settlement import SettlementService
from app.services.switch_engine import SwitchEngine
from app.services.telemetry import TelemetryService
from app.workers.sla_sweeper import SlaSweeper
from cbs.config import CbsSettings
from cbs.db.models import CbsBase
from cbs.db.seed import seed_cbs_database
from cbs.main import create_cbs_app
from cbs.services.core_engine import CbsCoreEngine
from shared.pki import ensure_pki_bundle
from shared.security import FieldEncryptor
from wallet_sim.main import WalletSimSettings, WalletStore, create_wallet_app


@dataclass
class MultiBankTestEnv:
    client: httpx.AsyncClient
    switch_session_factory: async_sessionmaker[AsyncSession]
    bci_session_factory: async_sessionmaker[AsyncSession]
    stdb_session_factory: async_sessionmaker[AsyncSession]
    bci_client: httpx.AsyncClient
    stdb_client: httpx.AsyncClient
    redis: redis.Redis
    sweeper: SlaSweeper
    certs_dir: Path
    settings: Settings
    admin_headers: dict[str, str]
    maker_headers: dict[str, str]
    checker_headers: dict[str, str]
    fraud_headers: dict[str, str]
    viewer_headers: dict[str, str]


async def _get_token_headers(
    client: httpx.AsyncClient, username: str, password: str
) -> dict[str, str]:
    resp = await client.post(
        "/oauth/token",
        json={"grant_type": "password", "username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def clean_env(tmp_path: Path) -> AsyncIterator[MultiBankTestEnv]:
    base_pg_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://metix:metix_secret@localhost:5433/metix_switch",
    )
    bci_pg_url = os.getenv(
        "TEST_BCI_DATABASE_URL",
        "postgresql+asyncpg://bci_user:bci_secret@localhost:5434/bci_cbs",
    )
    stdb_pg_url = os.getenv(
        "TEST_STDB_DATABASE_URL",
        "postgresql+asyncpg://stdb_user:stdb_secret@localhost:5435/stdb_cbs",
    )
    redis_url = os.getenv("TEST_REDIS_URL", "redis://localhost:6380/1")

    certs_dir = tmp_path / "certs"
    ensure_pki_bundle(certs_dir)

    switch_settings = Settings(
        database_url=base_pg_url,
        redis_url=redis_url,
        bci_cbs_base_url="http://bci-cbs",
        stdb_cbs_base_url="http://stdb-cbs",
        wallet_sim_base_url="http://wallet-sim",
        certs_dir=str(certs_dir),
        mtls_enabled=False,
        auth_enforce_transfers=True,
        spim_sla_seconds=25,
        individual_daily_limit_centavos=20_000_000,
        business_daily_limit_centavos=50_000_000,
        run_inline_sweeper=False,
    )
    bci_settings = CbsSettings(
        bank_code="BCI_MZ",
        bank_name="Banco Comercial e de Investimentos",
        bic_code="CGDIMZMA",
        service_id="bci-cbs",
        database_url=bci_pg_url,
        redis_url=redis_url,
        certs_dir=str(certs_dir),
        mtls_enabled=False,
        require_signed_requests=True,
    )
    stdb_settings = CbsSettings(
        bank_code="STDB_MZ",
        bank_name="Standard Bank Moçambique",
        bic_code="SBICMZMX",
        service_id="stdb-cbs",
        database_url=stdb_pg_url,
        redis_url=redis_url,
        certs_dir=str(certs_dir),
        mtls_enabled=False,
        require_signed_requests=True,
    )
    wallet_settings = WalletSimSettings(
        service_id="wallet-sim",
        redis_url=redis_url,
        certs_dir=str(certs_dir),
        mtls_enabled=False,
        require_signed_requests=True,
    )

    # 1. Initialize & clean all 3 completely independent databases
    sw_engine = create_async_engine(switch_settings.database_url, echo=False)
    bci_engine = create_async_engine(bci_settings.database_url, echo=False)
    stdb_engine = create_async_engine(stdb_settings.database_url, echo=False)

    async with sw_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with bci_engine.begin() as conn:
        await conn.run_sync(CbsBase.metadata.drop_all)
        await conn.run_sync(CbsBase.metadata.create_all)
    async with stdb_engine.begin() as conn:
        await conn.run_sync(CbsBase.metadata.drop_all)
        await conn.run_sync(CbsBase.metadata.create_all)

    sw_sf = async_sessionmaker(bind=sw_engine, class_=AsyncSession, expire_on_commit=False)
    bci_sf = async_sessionmaker(bind=bci_engine, class_=AsyncSession, expire_on_commit=False)
    stdb_sf = async_sessionmaker(bind=stdb_engine, class_=AsyncSession, expire_on_commit=False)

    async with sw_sf() as s:
        await seed_database(s)
    async with bci_sf() as s:
        await seed_cbs_database(
            s, bank_code="BCI_MZ", encryptor=FieldEncryptor(bci_settings.encryption_key)
        )
    async with stdb_sf() as s:
        await seed_cbs_database(
            s, bank_code="STDB_MZ", encryptor=FieldEncryptor(stdb_settings.encryption_key)
        )

    redis_client = redis.from_url(redis_url, decode_responses=False)
    await redis_client.flushdb()

    # 2. Wire independent ASGI apps for BCI CBS, Standard Bank CBS, and Mobile Wallets
    bci_app = create_cbs_app(bci_settings)
    bci_app.state.settings = bci_settings
    bci_app.state.session_factory = bci_sf
    bci_app.state.redis = redis_client
    bci_app.state.cbs_engine = CbsCoreEngine(bci_sf, redis_client, bci_settings)

    stdb_app = create_cbs_app(stdb_settings)
    stdb_app.state.settings = stdb_settings
    stdb_app.state.session_factory = stdb_sf
    stdb_app.state.redis = redis_client
    stdb_app.state.cbs_engine = CbsCoreEngine(stdb_sf, redis_client, stdb_settings)

    wallet_app = create_wallet_app(wallet_settings)
    wallet_app.state.settings = wallet_settings
    wallet_app.state.redis = redis_client
    wallet_store = WalletStore(redis_client)
    await wallet_store.seed()
    wallet_app.state.wallet_store = wallet_store

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=bci_app), base_url="http://bci-cbs"
        ) as bci_http,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stdb_app), base_url="http://stdb-cbs"
        ) as stdb_http,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wallet_app), base_url="http://wallet-sim"
        ) as wallet_http,
    ):
        registry = AdapterRegistry(
            {
                "BCI_MZ": BciCoreBankingAdapter(
                    switch_settings, redis_client=redis_client, http_client=bci_http
                ),
                "STDB_MZ": StandardBankCoreBankingAdapter(
                    switch_settings, redis_client=redis_client, http_client=stdb_http
                ),
                "MPESA_MZ": MobileWalletAdapter(
                    bank_code="MPESA_MZ",
                    bank_name="Vodacom M-Pesa Moçambique",
                    settings=switch_settings,
                    redis_client=redis_client,
                    http_client=wallet_http,
                ),
                "EMOLA_MZ": MobileWalletAdapter(
                    bank_code="EMOLA_MZ",
                    bank_name="Movitel e-Mola Moçambique",
                    settings=switch_settings,
                    redis_client=redis_client,
                    http_client=wallet_http,
                ),
            }
        )

        sw_app = create_app(settings=switch_settings, adapter_registry=registry)
        settlement_svc = SettlementService(sw_sf, switch_settings)
        recon_svc = ReconciliationService(sw_sf, registry)
        mc_svc = MakerCheckerService(sw_sf, registry, settlement_svc)
        telemetry_svc = TelemetryService(sw_sf)
        sweeper = SlaSweeper(sw_sf, redis_client, registry, switch_settings)

        sw_app.state.settings = switch_settings
        sw_app.state.session_factory = sw_sf
        sw_app.state.redis = redis_client
        sw_app.state.adapter_registry = registry
        sw_app.state.switch_engine = SwitchEngine(sw_sf, redis_client, registry, switch_settings)
        sw_app.state.sla_sweeper = sweeper
        sw_app.state.settlement_service = settlement_svc
        sw_app.state.reconciliation_service = recon_svc
        sw_app.state.maker_checker_service = mc_svc
        sw_app.state.telemetry_service = telemetry_svc

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=sw_app), base_url="http://switch-api"
        ) as sw_client:
            admin_h = await _get_token_headers(sw_client, "sec.admin", "admin-mz-2026")
            maker_h = await _get_token_headers(sw_client, "ops.maker", "maker-mz-2026")
            checker_h = await _get_token_headers(sw_client, "ops.checker", "checker-mz-2026")
            fraud_h = await _get_token_headers(sw_client, "fraud.analyst", "fraud-mz-2026")
            viewer_h = await _get_token_headers(sw_client, "ops.viewer", "viewer-mz-2026")

            # Fast normal chaos latency for tests (5ms)
            for bank in ("BCI_MZ", "STDB_MZ", "MPESA_MZ", "EMOLA_MZ"):
                r = await sw_client.post(
                    f"/api/v1/banks/{bank}/chaos",
                    json={"preset": "normal", "latency_ms": 5},
                    headers=admin_h,
                )
                assert r.status_code == 200

            yield MultiBankTestEnv(
                client=sw_client,
                switch_session_factory=sw_sf,
                bci_session_factory=bci_sf,
                stdb_session_factory=stdb_sf,
                bci_client=bci_http,
                stdb_client=stdb_http,
                redis=redis_client,
                sweeper=sweeper,
                certs_dir=certs_dir,
                settings=switch_settings,
                admin_headers=admin_h,
                maker_headers=maker_h,
                checker_headers=checker_h,
                fraud_headers=fraud_h,
                viewer_headers=viewer_h,
            )

    await redis_client.flushdb()
    await redis_client.aclose()
    await sw_engine.dispose()
    await bci_engine.dispose()
    await stdb_engine.dispose()
