"""
main.py — FastAPI Application Entry Point Mounting All v1 Routers & Lifespan SQLite WAL Init.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from api.routes import analytics, auth, requests, shifts, timelogs
from database import close_db, get_session, init_db
from models import Hospital, Shift, ShiftRequest, ShiftStatus, User, Workplace


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


@app.get(f"{API_V1_PREFIX}/health", tags=["System Health"])
@app.get("/health", tags=["System Health"])
async def health_check() -> dict[str, str]:
    """Healthcheck endpoint used by Docker Compose and Host Caddy."""
    return {
        "status": "ok",
        "engine": "sqlite+aiosqlite (WAL, FK=ON)",
    }


@app.get(f"{API_V1_PREFIX}/bootstrap", tags=["Bootstrap"])
async def bootstrap(session: AsyncSession = Depends(get_session)) -> dict:
    """Returns all master data (hospitals, workplaces, physicians, shifts, and swap requests) for frontend initialization."""
    hospitals = (await session.execute(select(Hospital).order_by(Hospital.id))).scalars().all()
    workplaces = (await session.execute(select(Workplace).order_by(Workplace.id))).scalars().all()
    doctors = (await session.execute(select(User).order_by(User.id))).scalars().all()
    shifts_res = (
        await session.execute(
            select(Shift).where(Shift.status != ShiftStatus.CANCELLED).order_by(Shift.start_ts, Shift.id)
        )
    ).scalars().all()
    requests_res = (
        await session.execute(select(ShiftRequest).order_by(ShiftRequest.updated_at.desc(), ShiftRequest.id.desc()))
    ).scalars().all()

    return {
        "hospitals": [
            {
                "id": h.id,
                "code": h.code,
                "name": h.name,
                "address": h.address,
                "requires24hIcuCoverage": h.requires_24h_icu_coverage,
            }
            for h in hospitals
        ],
        "workplaces": [
            {
                "id": w.id,
                "hospitalId": w.hospital_id,
                "code": w.code,
                "name": w.name,
                "type": w.workplace_type.value,
                "requiredSkills": w.required_skills,
            }
            for w in workplaces
        ],
        "doctors": [
            {
                "id": d.id,
                "fullName": d.full_name,
                "email": d.email,
                "role": d.role.value,
                "primaryHospitalId": d.assigned_hospital_id or (hospitals[0].id if hospitals else 1),
                "assignedHospitalId": d.assigned_hospital_id,
                "primarySkill": d.skills[0] if d.skills else "General Anesthesia",
                "skills": d.skills,
                "contractWeeklyHours": d.contract_weekly_hours,
            }
            for d in doctors
        ],
        "shifts": [
            {
                "id": s.id,
                "shiftType": s.shift_type.value,
                "parentShiftId": s.parent_shift_id,
                "doctorId": s.doctor_id,
                "hospitalId": s.hospital_id,
                "workplaceId": s.workplace_id,
                "startTs": s.start_ts,
                "endTs": s.end_ts,
                "status": s.status.value,
                "is24hIcuDuty": s.is_24h_icu_duty,
                "isFatigueRisk": s.is_fatigue_risk,
                "continuousHoursAtEnd": s.continuous_hours_at_end,
                "fatigueRiskHours": s.fatigue_risk_hours,
                "notes": s.notes or "",
            }
            for s in shifts_res
        ],
        "swapRequests": [
            {
                "id": r.id,
                "requestType": r.request_type.value,
                "status": r.status.value,
                "hospitalId": r.hospital_id,
                "requesterId": r.requester_id,
                "targetDoctorId": r.target_doctor_id,
                "sourceShiftId": r.source_shift_id,
                "targetShiftId": r.target_shift_id,
                "reason": r.reason or "",
            }
            for r in requests_res
        ],
    }


app.include_router(auth.router, prefix=API_V1_PREFIX)
app.include_router(shifts.router, prefix=API_V1_PREFIX)
app.include_router(requests.router, prefix=API_V1_PREFIX)
app.include_router(timelogs.router, prefix=API_V1_PREFIX)
app.include_router(analytics.router, prefix=API_V1_PREFIX)
