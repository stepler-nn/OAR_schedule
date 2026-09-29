"""
main.py — FastAPI Application Entry Point Mounting All v1 Routers & Lifespan SQLite WAL Init.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import analytics, auth, requests, shifts, timelogs
from database import close_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initializes SQLite WAL mode & Foreign Keys on startup and disposes pool on shutdown."""
    await init_db()
    yield
    await close_db()


app = FastAPI(
    title="ChronoMed Anesthesiology & ICU Scheduling API",
    version="1.0.0",
    description=(
        "Self-hosted Modular Monolith API for 30 physicians across 4 hospitals, "
        "enforcing parent-child OR/ICU shift constraints, 32h fatigue tracking, "
        "and atomic FSM shift swaps."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_V1_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=API_V1_PREFIX)
app.include_router(shifts.router, prefix=API_V1_PREFIX)
app.include_router(requests.router, prefix=API_V1_PREFIX)
app.include_router(timelogs.router, prefix=API_V1_PREFIX)
app.include_router(analytics.router, prefix=API_V1_PREFIX)
