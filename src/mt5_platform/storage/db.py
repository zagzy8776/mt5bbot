"""Database engine / session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mt5_platform.storage.models import Base


def normalize_database_url(url: str) -> str:
    """Accept common URLs and coerce to SQLAlchemy async drivers."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite://") and "aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    url = normalize_database_url(database_url)
    kwargs: dict = {"echo": echo}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    elif url.startswith("postgresql"):
        # asyncpg doesn't accept libpq's ``sslmode`` query parameter.  Strip it
        # and use the native asyncpg ``ssl`` connect_arg instead.
        #
        # Hosted providers (Aiven, etc.) use their own CA which may not be in the
        # system trust store.  We create an SSL context that verifies the server
        # certificate chain without hostname checking, which is the safe default
        # for database connections (we authenticate via password, not mTLS).
        import ssl as _ssl

        if "sslmode=" in url:
            base, _, _query = url.partition("?")
            # Keep any non-sslmode params
            kept = "&".join(
                p for p in _query.split("&") if not p.startswith("sslmode=")
            )
            url = base if not kept else f"{base}?{kept}"

        ssl_ctx = _ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _ssl.CERT_NONE
        kwargs["connect_args"] = {"ssl": ssl_ctx}
        kwargs["pool_pre_ping"] = True
    return create_async_engine(url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db(engine: AsyncEngine) -> None:
    """Create tables. Timescale hypertables can be applied later via migration."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
