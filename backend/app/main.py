from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.cbs_adapters import BciCoreBankingAdapter, StandardBankCoreBankingAdapter
from app.adapters.registry import AdapterRegistry
from app.adapters.wallet_adapter import MobileWalletAdapter
from app.api.auth_routes import router as auth_router
from app.api.ops_routes import router as ops_router
from app.api.switch_routes import router as switch_router
from app.config import Settings, get_settings
from app.db.seed import seed_database
from app.db.session import dispose_engine, get_session_factory, init_db
from app.services.maker_checker import MakerCheckerService
from app.services.reconciliation import ReconciliationService
from app.services.settlement import SettlementService
from app.services.switch_engine import SwitchEngine
from app.services.telemetry import TelemetryService
from app.workers.sla_sweeper import SlaSweeper
from shared.pki import ensure_pki_bundle


def build_default_adapter_registry(cfg: Settings, redis_client: redis.Redis) -> AdapterRegistry:
    return AdapterRegistry(
        {
            "BCI_MZ": BciCoreBankingAdapter(cfg, redis_client=redis_client),
            "STDB_MZ": StandardBankCoreBankingAdapter(cfg, redis_client=redis_client),
            "MPESA_MZ": MobileWalletAdapter(
                bank_code="MPESA_MZ",
                bank_name="Vodacom M-Pesa Moçambique",
                settings=cfg,
                redis_client=redis_client,
            ),
            "EMOLA_MZ": MobileWalletAdapter(
                bank_code="EMOLA_MZ",
                bank_name="Movitel e-Mola Moçambique",
                settings=cfg,
                redis_client=redis_client,
            ),
        }
    )


def create_app(
    settings: Settings | None = None,
    *,
    adapter_registry: AdapterRegistry | None = None,
) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ensure_pki_bundle(cfg.certs_dir)
        await init_db()
        session_factory = get_session_factory()
        async with session_factory() as session:
            await seed_database(session)

        redis_client = redis.from_url(cfg.redis_url, decode_responses=False)
        registry = adapter_registry or build_default_adapter_registry(cfg, redis_client)
        settlement_svc = SettlementService(session_factory, cfg)
        recon_svc = ReconciliationService(session_factory, registry)
        mc_svc = MakerCheckerService(session_factory, registry, settlement_svc)
        telemetry_svc = TelemetryService(session_factory)

        app.state.settings = cfg
        app.state.session_factory = session_factory
        app.state.redis = redis_client
        app.state.adapter_registry = registry
        app.state.switch_engine = SwitchEngine(session_factory, redis_client, registry, cfg)
        app.state.sla_sweeper = SlaSweeper(session_factory, redis_client, registry, cfg)
        app.state.settlement_service = settlement_svc
        app.state.reconciliation_service = recon_svc
        app.state.maker_checker_service = mc_svc
        app.state.telemetry_service = telemetry_svc

        sweeper_task: asyncio.Task[None] | None = None
        stop_event = asyncio.Event()
        if cfg.run_inline_sweeper:
            sweeper_task = asyncio.create_task(app.state.sla_sweeper.run_forever(stop_event))

        try:
            yield
        finally:
            if sweeper_task is not None:
                stop_event.set()
                sweeper_task.cancel()
                with suppress(asyncio.CancelledError):
                    await sweeper_task
            await redis_client.aclose()
            await dispose_engine()

    app = FastAPI(
        title="METIX / SIMOrede Interbank Instant Payment Switch",
        description=(
            "Multi-service ISO 8583 instant payment switch communicating with independent BCI "
            "and Standard Bank Core Banking Simulators over mTLS, OAuth 2.0, and Ed25519."
        ),
        version="2.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(switch_router)
    app.include_router(auth_router)
    app.include_router(ops_router)
    return app


app = create_app()
