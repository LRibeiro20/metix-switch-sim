from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.beneficiary_sim import router as beneficiary_router
from app.api.switch_routes import router as switch_router
from app.config import Settings, get_settings
from app.db.seed import seed_database
from app.db.session import dispose_engine, get_session_factory, init_db
from app.services.switch_engine import HttpBeneficiaryClient, SwitchEngine
from app.workers.sla_sweeper import SlaSweeper


def create_app(
    settings: Settings | None = None,
    *,
    beneficiary_client: HttpBeneficiaryClient | None = None,
) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await init_db()
        session_factory = get_session_factory()
        async with session_factory() as session:
            await seed_database(session)

        redis_client = redis.from_url(cfg.redis_url, decode_responses=False)
        ben_client = beneficiary_client or HttpBeneficiaryClient(cfg.beneficiary_base_url)

        app.state.settings = cfg
        app.state.session_factory = session_factory
        app.state.redis = redis_client
        app.state.beneficiary_client = ben_client
        app.state.switch_engine = SwitchEngine(session_factory, redis_client, ben_client, cfg)
        app.state.sla_sweeper = SlaSweeper(session_factory, redis_client, ben_client, cfg)

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
        title="METIX / SIMOrede Instant Payment Switch Simulator",
        description=(
            "Fault-tolerant ISO 8583 & REST switch modeled on Banco de Moçambique "
            "Notice No. 1/GBM/2026."
        ),
        version="0.1.0",
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
    app.include_router(beneficiary_router)
    return app


app = create_app()
