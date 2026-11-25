from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI

from cbs.api.routes import router as cbs_router
from cbs.config import CbsSettings, get_cbs_settings
from cbs.db.seed import seed_cbs_database
from cbs.db.session import create_cbs_engine, create_cbs_session_factory, init_cbs_db
from cbs.services.core_engine import CbsCoreEngine
from shared.pki import ensure_pki_bundle
from shared.security import FieldEncryptor


def create_cbs_app(settings: CbsSettings | None = None) -> FastAPI:
    cfg = settings or get_cbs_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ensure_pki_bundle(cfg.certs_dir)
        engine = create_cbs_engine(cfg.database_url)
        await init_cbs_db(engine)
        session_factory = create_cbs_session_factory(engine)
        encryptor = FieldEncryptor(cfg.encryption_key)
        async with session_factory() as session:
            await seed_cbs_database(session, bank_code=cfg.bank_code, encryptor=encryptor)

        redis_client = redis.from_url(cfg.redis_url, decode_responses=False)
        app.state.settings = cfg
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.redis = redis_client
        app.state.cbs_engine = CbsCoreEngine(session_factory, redis_client, cfg)
        try:
            yield
        finally:
            await redis_client.aclose()
            await engine.dispose()

    app = FastAPI(
        title=f"{cfg.bank_name} Core Banking Simulator ({cfg.bank_code})",
        description=f"Independent Core Banking System for {cfg.bank_code} ({cfg.bic_code}).",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(cbs_router)
    return app


app = create_cbs_app()
