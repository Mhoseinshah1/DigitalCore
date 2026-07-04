"""Shared pytest fixtures.

Provides an httpx AsyncClient bound to the FastAPI app, and an async DB session.
The DB session prefers a real Postgres via TEST_DATABASE_URL and transparently
falls back to an in-memory SQLite database when Postgres is unavailable.
"""
from __future__ import annotations

import os

import httpx
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.web.main import app


@pytest_asyncio.fixture
async def client() -> httpx.AsyncClient:
    """An httpx AsyncClient that talks to app.web.main:app in-process."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _build_engine() -> AsyncEngine:
    """Prefer TEST_DATABASE_URL (Postgres); fall back to in-memory SQLite."""
    test_url = os.getenv("TEST_DATABASE_URL")
    if test_url:
        engine = create_async_engine(test_url, pool_pre_ping=True)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return engine
        except Exception:
            await engine.dispose()
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """An async session against a fresh schema (test-only create_all)."""
    engine = await _build_engine()
    # Test schema only — production uses Alembic migrations, never create_all.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()
