"""
database.py — Async SQLite Database Engine & Session Configuration (aiosqlite + SQLModel).

Infrastructure Highlights:
1. Uses `sqlite+aiosqlite` for non-blocking async I/O in FastAPI.
2. Attaches a connection-level SQLAlchemy event listener on `engine.sync_engine` so that
   EVERY new SQLite connection automatically executes:
     - `PRAGMA journal_mode=WAL;`   (Write-Ahead Logging for concurrent reads + single writer)
     - `PRAGMA foreign_keys=ON;`    (Strict relational integrity for Parent-Child shifts & RBAC)
     - `PRAGMA synchronous=NORMAL;` (Safe & fast fsync behavior tailored for WAL mode)
     - `PRAGMA busy_timeout=5000;`  (5-second lock wait to prevent SQLITE_BUSY under swap bursts)
     - `PRAGMA temp_store=MEMORY;`  (In-memory temporary B-trees for analytical overlap queries)
3. Provides `get_session()` dependency for FastAPI routes and an `atomic_transaction()` helper
   used when committing `APPROVED` Shift Swaps alongside their `AuditLog` entries.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

# Import models so SQLModel.metadata registers all tables before init_db() runs
import models  # noqa: F401

# Default self-hosted SQLite database file path (override via DATABASE_URL env var in Docker)
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./data/anesthesia_icu_scheduler.db",
)

# Create the AsyncEngine for SQLite via aiosqlite
engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=os.getenv("SQL_ECHO", "false").lower() == "true",
    future=True,
    connect_args={
        # Allow aiosqlite worker thread to share connection safely with FastAPI's event loop
        "check_same_thread": False,
        "timeout": 15.0,
    },
)


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:
    """
    Executed automatically on every new SQLite physical connection.
    Enforces WAL mode for high concurrency (30 doctors checking in via PWA while
    Senior Residents edit OR schedules) and enables strict Foreign Key enforcement.
    """
    cursor = dbapi_connection.cursor()
    try:
        # 1. Enable Write-Ahead Logging (readers do not block writers; writers do not block readers)
        cursor.execute("PRAGMA journal_mode=WAL;")
        # 2. Enforce Foreign Key constraints (SQLite disables FKs by default per connection!)
        cursor.execute("PRAGMA foreign_keys=ON;")
        # 3. Optimize durability/performance balance for WAL mode
        cursor.execute("PRAGMA synchronous=NORMAL;")
        # 4. Wait up to 5000ms if another transaction holds a write lock before raising SQLITE_BUSY
        cursor.execute("PRAGMA busy_timeout=5000;")
        # 5. Store temporary indices/tables in RAM for faster fatigue & coverage aggregations
        cursor.execute("PRAGMA temp_store=MEMORY;")
    finally:
        cursor.close()


# Async session factory configured with expire_on_commit=False for FastAPI async handlers
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def init_db() -> None:
    """
    Initializes the database directory, creates all SQLModel tables & composite indexes,
    and verifies that WAL mode and Foreign Keys are active.
    Call this inside FastAPI's lifespan startup context.
    """
    # Ensure parent folder exists if using a local file path
    if DATABASE_URL.startswith("sqlite+aiosqlite:///"):
        raw_path = DATABASE_URL.replace("sqlite+aiosqlite:///", "", 1)
        db_dir = os.path.dirname(os.path.abspath(raw_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

        # Verify PRAGMAs on startup
        journal_mode = (await conn.execute(text("PRAGMA journal_mode;"))).scalar_one()
        fk_enabled = (await conn.execute(text("PRAGMA foreign_keys;"))).scalar_one()
        if str(journal_mode).lower() != "wal":
            raise RuntimeError(f"Expected SQLite WAL mode, got: {journal_mode}")
        if int(fk_enabled) != 1:
            raise RuntimeError("SQLite PRAGMA foreign_keys failed to enable.")


async def close_db() -> None:
    """Cleanly disposes the async SQLite connection pool on application shutdown."""
    await engine.dispose()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI Dependency that yields a scoped AsyncSession per HTTP request.
    Automatically rolls back uncommitted changes if an unhandled exception occurs.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def atomic_transaction(session: AsyncSession) -> AsyncGenerator[AsyncSession, None]:
    """
    Explicit atomic transaction boundary for critical domain operations
    (e.g., transitioning a ShiftRequest to APPROVED, swapping Shift.doctor_id on both
    parent Base Shifts and child Operational Tasks, and appending an AuditLog).

    Usage:
        async with atomic_transaction(session):
            shift_a.doctor_id, shift_b.doctor_id = shift_b.doctor_id, shift_a.doctor_id
            swap_request.transition_to(RequestStatus.APPROVED)
            session.add(audit_entry)
    """
    if session.in_transaction():
        async with session.begin_nested():
            yield session
        await session.commit()
    else:
        async with session.begin():
            yield session
