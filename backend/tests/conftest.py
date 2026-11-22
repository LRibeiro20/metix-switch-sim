from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
import redis.asyncio as redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import Base
from app.db.seed import seed_database
from app.main import create_app
from app.services.switch_engine import HttpBeneficiaryClient, SwitchEngine
from app.workers.sla_sweeper import SlaSweeper


@pytest_asyncio.fixture
async def test_settings() -> Settings:
    return Settings(
        database_url=os.getenv(
            "TEST_DATABASE_URL",
            "postgresql+asyncpg://metix:metix_secret@localhost:5433/metix_switch",
        ),
        redis_url=os.getenv("TEST_REDIS_URL", "redis://localhost:6380/1"),
        spim_sla_seconds=25,
        individual_daily_limit_centavos=20_000_000,
        business_daily_limit_centavos=50_000_000,
        beneficiary_base_url="http://testserver",
        run_inline_sweeper=False,
    )


@pytest_asyncio.fixture
async def clean_env(
    test_settings: Settings,
) -> AsyncIterator[
    tuple[
        httpx.AsyncClient,
        async_sessionmaker[AsyncSession],
        redis.Redis,
        SlaSweeper,
    ]
]:
    engine = create_async_engine(test_settings.database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "TRUNCATE TABLE iso8583_message_log, journal_postings, journal_entries, "
                "switch_transactions, accounts, institutions RESTART IDENTITY CASCADE"
            )
        )

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with session_factory() as session:
        await seed_database(session)

    redis_client = redis.from_url(test_settings.redis_url, decode_responses=False)
    await redis_client.flushdb()

    app = create_app(settings=test_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        ben_client = HttpBeneficiaryClient("http://testserver", http_client=client)
        app.state.settings = test_settings
        app.state.session_factory = session_factory
        app.state.redis = redis_client
        app.state.beneficiary_client = ben_client
        app.state.switch_engine = SwitchEngine(
            session_factory, redis_client, ben_client, test_settings
        )
        sweeper = SlaSweeper(session_factory, redis_client, ben_client, test_settings)
        app.state.sla_sweeper = sweeper

        yield client, session_factory, redis_client, sweeper

    await redis_client.flushdb()
    await redis_client.aclose()
    await engine.dispose()
