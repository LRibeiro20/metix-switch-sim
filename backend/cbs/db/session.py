from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cbs.db.models import CbsBase


def create_cbs_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=15,
        max_overflow=10,
    )


def create_cbs_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def init_cbs_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(CbsBase.metadata.create_all)
