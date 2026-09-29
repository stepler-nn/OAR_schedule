export const MODELS_PY_CODE = `"""
models.py — SQLModel Schema Definitions for Anesthesiology & ICU Scheduling System.

Domain Architecture & Constraints Encoded:
1. Scale: ~30 Doctors across 4 Hospitals (~5 Operating Rooms + 1 ICU Duty role per hospital).
2. Temporal Storage: All timestamps are stored as UTC Unix Timestamps (INTEGER seconds)
   to enable ultra-fast mathematical interval overlap checks:
   (StartA < EndB AND EndA > StartB) using composite B-Tree indexes.
3. Parent-Child Shifts:
   - BASE_SHIFT (Parent): e.g., 24-hour ICU Duty at Hospital A, or 8h-16h Daytime OR shift.
   - OPERATIONAL_TASK (Child): e.g., 08:00-16:00 in OR #2 at Hospital A, linked via
     \`parent_shift_id\` to the Base Shift.
   - Physical Restriction: Child task \`hospital_id\` MUST match Parent shift \`hospital_id\`,
     and Child interval \`[start_ts, end_ts]\` MUST be contained within Parent \`[start_ts, end_ts]\`.
4. Fatigue Tracking:
   - Continuous shifts up to 32 hours (e.g., 24h ICU duty + 8h daytime continuation) are
     permitted by the domain, but any hours exceeding 24 continuous hours are flagged with
     \`is_fatigue_risk = True\` and \`fatigue_risk_hours > 0\`.
5. Shift Swap & Request Finite State Machine (FSM):
   - States: DRAFT -> PROPOSED -> PEER_ACCEPTED / PEER_REJECTED ->
     APPROVED / REJECTED_BY_MANAGER / REVISION_REQUESTED -> CANCELLED.
   - Shifts in the database are mutated atomically ONLY when a request transitions to \`APPROVED\`.
6. RBAC Roles:
   - DOCTOR: Global schedule read, PWA check-in/out, create/respond to swap & revision requests.
   - SENIOR_RESIDENT: Full shift/task CRUD and request approval scoped strictly to \`assigned_hospital_id\`.
   - HEAD_OF_DEPT: Global CRUD across all hospitals, user management, fatigue analytics & exports.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Dict, List, Optional, Set

from sqlalchemy import CheckConstraint, Column, Index, JSON, Text
from sqlmodel import Field, Relationship, SQLModel


def utc_now_ts() -> int:
    """Returns current UTC time as a Unix epoch integer (seconds)."""
    return int(time.time())


# =====================================================================
# 1. DOMAIN ENUMERATIONS
# =====================================================================

class UserRole(str, Enum):
    """Role-Based Access Control (RBAC) tiers."""
    DOCTOR = "DOCTOR"
    SENIOR_RESIDENT = "SENIOR_RESIDENT"  # Hospital-scoped manager (requires assigned_hospital_id)
    HEAD_OF_DEPT = "HEAD_OF_DEPT"        # Global manager & department administrator


class WorkplaceType(str, Enum):
    """Physical clinical unit classification inside a hospital."""
    OR = "OR"    # Operating Room (typically ~5 per hospital, daytime 8h-16h or sub-tasks)
    ICU = "ICU"  # Intensive Care Unit duty station (requires uninterrupted 24h coverage)


class ShiftType(str, Enum):
    """Distinguishes top-level shifts from nested clinical sub-allocations."""
    BASE_SHIFT = "BASE_SHIFT"              # Parent shift (e.g., 24h ICU Duty or 8h-16h Daytime Shift)
    OPERATIONAL_TASK = "OPERATIONAL_TASK"  # Child task (e.g., 08:00-16:00 OR #2 linked to a 24h ICU Base Shift)


class ShiftStatus(str, Enum):
    """Lifecycle status of an individual shift or operational task."""
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class RequestType(str, Enum):
    """Category of doctor-initiated schedule request."""
    SHIFT_SWAP = "SHIFT_SWAP"  # Peer-to-peer swap requiring peer acceptance + manager approval
    VACATION = "VACATION"      # Unavailability / leave request requiring manager approval
    REVISION = "REVISION"      # Doctor challenge/revision request on an assigned shift


class RequestStatus(str, Enum):
    """
    Shift Swap & Schedule Request Finite State Machine (FSM) States.

    Canonical Transition Path:
    DRAFT -> PROPOSED -> PEER_ACCEPTED / PEER_REJECTED ->
    APPROVED / REJECTED_BY_MANAGER / REVISION_REQUESTED -> CANCELLED

    CRITICAL INVARIANT:
    Actual \`Shift.doctor_id\` records in SQLite are updated atomically ONLY
    upon transitioning into \`APPROVED\`.
    """
    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    PEER_ACCEPTED = "PEER_ACCEPTED"
    PEER_REJECTED = "PEER_REJECTED"
    APPROVED = "APPROVED"
    REJECTED_BY_MANAGER = "REJECTED_BY_MANAGER"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    CANCELLED = "CANCELLED"


# Explicit transition table enforcing valid FSM state mutations
ALLOWED_FSM_TRANSITIONS: Dict[RequestStatus, Set[RequestStatus]] = {
    RequestStatus.DRAFT: {
        RequestStatus.PROPOSED,
        RequestStatus.CANCELLED,
    },
    RequestStatus.PROPOSED: {
        RequestStatus.PEER_ACCEPTED,
        RequestStatus.PEER_REJECTED,
        RequestStatus.REVISION_REQUESTED,
        RequestStatus.CANCELLED,
    },
    RequestStatus.PEER_ACCEPTED: {
        RequestStatus.APPROVED,
        RequestStatus.REJECTED_BY_MANAGER,
        RequestStatus.REVISION_REQUESTED,
        RequestStatus.CANCELLED,
    },
    RequestStatus.PEER_REJECTED: {
        RequestStatus.REVISION_REQUESTED,
        RequestStatus.CANCELLED,
    },
    RequestStatus.REVISION_REQUESTED: {
        RequestStatus.PROPOSED,
        RequestStatus.CANCELLED,
    },
    RequestStatus.REJECTED_BY_MANAGER: {
        RequestStatus.REVISION_REQUESTED,
        RequestStatus.CANCELLED,
    },
    # Terminal states: APPROVED and CANCELLED cannot transition further
    RequestStatus.APPROVED: set(),
    RequestStatus.CANCELLED: set(),
}


class AuditAction(str, Enum):
    """Categorizes immutable audit trail entries."""
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    FSM_TRANSITION = "FSM_TRANSITION"
    ATOMIC_SWAP_COMMIT = "ATOMIC_SWAP_COMMIT"
    CHECK_IN = "CHECK_IN"
    CHECK_OUT = "CHECK_OUT"
    FATIGUE_OVERRIDE = "FATIGUE_OVERRIDE"


# =====================================================================
# 2. HOSPITAL & WORKPLACE MODELS
# =====================================================================

class Hospital(SQLModel, table=True):
    """
    Represents one of the hospitals served by the Anesthesiology & ICU Department.
    Each hospital requires continuous 24h ICU duty coverage and hosts ~5 ORs.
    """
    __tablename__ = "hospitals"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(
        index=True,
        unique=True,
        max_length=16,
        description="Short identifier, e.g., 'HOSP-A', 'HOSP-B', 'HOSP-C', 'HOSP-D'",
    )
    name: str = Field(max_length=120, description="Full clinical facility name")
    address: Optional[str] = Field(default=None, max_length=255)
    requires_24h_icu_coverage: bool = Field(
        default=True,
        description="If True, the coverage validation engine flags any day lacking an active 24h ICU Base Shift",
    )
    is_active: bool = Field(default=True, index=True)
    created_at: int = Field(default_factory=utc_now_ts, nullable=False)

    # Relationships
    workplaces: List["Workplace"] = Relationship(back_populates="hospital")
    assigned_managers: List["User"] = Relationship(back_populates="assigned_hospital")
    shifts: List["Shift"] = Relationship(back_populates="hospital")
    requests: List["ShiftRequest"] = Relationship(back_populates="hospital")


class Workplace(SQLModel, table=True):
    """
    Represents an individual Operating Room (e.g., OR #1..OR #5) or ICU Duty Station
    physically located inside a specific Hospital.
    """
    __tablename__ = "workplaces"
    __table_args__ = (
        Index("ix_workplaces_hospital_type", "hospital_id", "workplace_type"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    hospital_id: int = Field(
        foreign_key="hospitals.id",
        index=True,
        nullable=False,
        description="Owning hospital; enforces physical location restrictions on child tasks",
    )
    code: str = Field(
        max_length=32,
        index=True,
        description="Unique room/station code within hospital, e.g., 'HOSP-A-OR-2' or 'HOSP-A-ICU-1'",
    )
    name: str = Field(max_length=100, description="Display label, e.g., 'Operating Room #2' or 'Main ICU Duty'")
    workplace_type: WorkplaceType = Field(
        index=True,
        description="OR for surgical rooms (~5/hospital) or ICU for 24h intensive care duty",
    )
    required_skills: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="List of specialty tags required to staff this workplace, e.g., ['CARDIAC_ANESTHESIA']",
    )
    is_active: bool = Field(default=True)
    created_at: int = Field(default_factory=utc_now_ts, nullable=False)

    # Relationships
    hospital: Optional[Hospital] = Relationship(back_populates="workplaces")
    shifts: List["Shift"] = Relationship(back_populates="workplace")


# =====================================================================
# 3. USER & RBAC MODEL
# =====================================================================

class User(SQLModel, table=True):
    """
    Represents a Physician in the Anesthesiology & ICU Department (~30 active doctors).
    Enforces RBAC scoping:
    - DOCTOR: Views all hospitals, checks in/out via PWA, submits/accepts swap requests.
    - SENIOR_RESIDENT: Hospital-scoped manager; CRUD restricted to \`assigned_hospital_id\`.
    - HEAD_OF_DEPT: Global administrator across all hospitals, user skills, and fatigue analytics.
    """
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "(role != 'SENIOR_RESIDENT') OR (assigned_hospital_id IS NOT NULL)",
            name="ck_senior_resident_requires_assigned_hospital",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True, max_length=160, nullable=False)
    full_name: str = Field(max_length=120, nullable=False)
    hashed_password: str = Field(max_length=255, nullable=False)

    role: UserRole = Field(
        default=UserRole.DOCTOR,
        index=True,
        nullable=False,
        description="RBAC permission tier: DOCTOR, SENIOR_RESIDENT, or HEAD_OF_DEPT",
    )
    assigned_hospital_id: Optional[int] = Field(
        default=None,
        foreign_key="hospitals.id",
        index=True,
        description="Mandatory for SENIOR_RESIDENT to scope CRUD & approval permissions to a single hospital",
    )

    skills: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="Doctor clinical qualifications, e.g., ['ICU_24H', 'NEURO_OR', 'PEDIATRIC_ANESTHESIA']",
    )
    contract_weekly_hours: int = Field(
        default=40,
        ge=8,
        le=80,
        description="Standard weekly contract baseline for overtime & fatigue reporting",
    )
    is_active: bool = Field(default=True, index=True)
    created_at: int = Field(default_factory=utc_now_ts, nullable=False)
    updated_at: int = Field(default_factory=utc_now_ts, nullable=False)

    # Relationships
    assigned_hospital: Optional[Hospital] = Relationship(back_populates="assigned_managers")
    shifts: List["Shift"] = Relationship(
        back_populates="doctor",
        sa_relationship_kwargs={"foreign_keys": "[Shift.doctor_id]"},
    )
    time_logs: List["TimeLog"] = Relationship(back_populates="doctor")
    initiated_requests: List["ShiftRequest"] = Relationship(
        back_populates="requester",
        sa_relationship_kwargs={"foreign_keys": "[ShiftRequest.requester_id]"},
    )
    targeted_requests: List["ShiftRequest"] = Relationship(
        back_populates="target_doctor",
        sa_relationship_kwargs={"foreign_keys": "[ShiftRequest.target_doctor_id]"},
    )


# =====================================================================
# 4. SHIFT MODEL (BASE SHIFTS & CHILD OPERATIONAL TASKS)
# =====================================================================

class Shift(SQLModel, table=True):
    """
    Unified Parent-Child Shift & Operational Task model.
    """
    __tablename__ = "shifts"
    __table_args__ = (
        CheckConstraint("end_ts > start_ts", name="ck_shift_positive_duration"),
        CheckConstraint(
            "(shift_type = 'BASE_SHIFT' AND parent_shift_id IS NULL) OR "
            "(shift_type = 'OPERATIONAL_TASK' AND parent_shift_id IS NOT NULL)",
            name="ck_shift_hierarchy_integrity",
        ),
        Index("ix_shifts_doctor_interval", "doctor_id", "start_ts", "end_ts"),
        Index("ix_shifts_hospital_interval", "hospital_id", "shift_type", "start_ts", "end_ts"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    shift_type: ShiftType = Field(default=ShiftType.BASE_SHIFT, index=True, nullable=False)
    parent_shift_id: Optional[int] = Field(default=None, foreign_key="shifts.id", index=True)

    doctor_id: int = Field(foreign_key="users.id", index=True, nullable=False)
    hospital_id: int = Field(foreign_key="hospitals.id", index=True, nullable=False)
    workplace_id: int = Field(foreign_key="workplaces.id", index=True, nullable=False)

    start_ts: int = Field(index=True, nullable=False)
    end_ts: int = Field(index=True, nullable=False)
    status: ShiftStatus = Field(default=ShiftStatus.SCHEDULED, index=True, nullable=False)

    is_24h_icu_duty: bool = Field(default=False, index=True)
    is_fatigue_risk: bool = Field(default=False, index=True)
    continuous_hours_at_end: float = Field(default=0.0)
    fatigue_risk_hours: float = Field(default=0.0)

    notes: Optional[str] = Field(default=None, max_length=500)
    created_by_id: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: int = Field(default_factory=utc_now_ts, nullable=False)
    updated_at: int = Field(default_factory=utc_now_ts, nullable=False)

    # Relationships
    doctor: Optional[User] = Relationship(
        back_populates="shifts",
        sa_relationship_kwargs={"foreign_keys": "[Shift.doctor_id]"},
    )
    hospital: Optional[Hospital] = Relationship(back_populates="shifts")
    workplace: Optional[Workplace] = Relationship(back_populates="shifts")

    parent_shift: Optional["Shift"] = Relationship(
        back_populates="operational_tasks",
        sa_relationship_kwargs={
            "remote_side": "Shift.id",
            "foreign_keys": "[Shift.parent_shift_id]",
        },
    )
    operational_tasks: List["Shift"] = Relationship(
        back_populates="parent_shift",
        sa_relationship_kwargs={
            "foreign_keys": "[Shift.parent_shift_id]",
            "cascade": "all, delete-orphan",
        },
    )
    time_logs: List["TimeLog"] = Relationship(back_populates="shift")


# =====================================================================
# 5. TIME LOG MODEL (PWA CHECK-IN / CHECK-OUT)
# =====================================================================

class TimeLog(SQLModel, table=True):
    __tablename__ = "time_logs"
    __table_args__ = (
        CheckConstraint(
            "check_out_ts IS NULL OR check_out_ts >= check_in_ts",
            name="ck_timelog_checkout_after_checkin",
        ),
        Index("ix_timelogs_doctor_checkin", "doctor_id", "check_in_ts"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    shift_id: int = Field(foreign_key="shifts.id", index=True, nullable=False)
    doctor_id: int = Field(foreign_key="users.id", index=True, nullable=False)

    check_in_ts: int = Field(nullable=False)
    check_out_ts: Optional[int] = Field(default=None)
    recorded_duration_seconds: Optional[int] = Field(default=None)
    is_fatigue_flagged: bool = Field(default=False, index=True)
    pwa_client_metadata: Optional[str] = Field(default=None, max_length=255)
    notes: Optional[str] = Field(default=None, max_length=500)
    created_at: int = Field(default_factory=utc_now_ts, nullable=False)

    shift: Optional[Shift] = Relationship(back_populates="time_logs")
    doctor: Optional[User] = Relationship(back_populates="time_logs")


# =====================================================================
# 6. SHIFT SWAP & VACATION REQUEST MODEL (FSM)
# =====================================================================

class ShiftRequest(SQLModel, table=True):
    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_hospital_status", "hospital_id", "status"),
        Index("ix_requests_requester_status", "requester_id", "status"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    request_type: RequestType = Field(default=RequestType.SHIFT_SWAP, index=True, nullable=False)
    status: RequestStatus = Field(default=RequestStatus.DRAFT, index=True, nullable=False)

    hospital_id: int = Field(foreign_key="hospitals.id", index=True, nullable=False)
    requester_id: int = Field(foreign_key="users.id", index=True, nullable=False)
    target_doctor_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)

    source_shift_id: Optional[int] = Field(default=None, foreign_key="shifts.id", index=True)
    target_shift_id: Optional[int] = Field(default=None, foreign_key="shifts.id", index=True)

    requested_start_ts: Optional[int] = Field(default=None)
    requested_end_ts: Optional[int] = Field(default=None)

    reason: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    peer_response_note: Optional[str] = Field(default=None, max_length=500)
    peer_responded_at: Optional[int] = Field(default=None)

    reviewed_by_id: Optional[int] = Field(default=None, foreign_key="users.id")
    manager_notes: Optional[str] = Field(default=None, max_length=500)
    reviewed_at: Optional[int] = Field(default=None)

    created_at: int = Field(default_factory=utc_now_ts, nullable=False)
    updated_at: int = Field(default_factory=utc_now_ts, nullable=False)

    hospital: Optional[Hospital] = Relationship(back_populates="requests")
    requester: Optional[User] = Relationship(
        back_populates="initiated_requests",
        sa_relationship_kwargs={"foreign_keys": "[ShiftRequest.requester_id]"},
    )
    target_doctor: Optional[User] = Relationship(
        back_populates="targeted_requests",
        sa_relationship_kwargs={"foreign_keys": "[ShiftRequest.target_doctor_id]"},
    )


# =====================================================================
# 7. IMMUTABLE AUDIT LOG MODEL
# =====================================================================

class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_entity_lookup", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_hospital_time", "hospital_id", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    actor_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    actor_role: Optional[UserRole] = Field(default=None)
    hospital_id: Optional[int] = Field(default=None, foreign_key="hospitals.id", index=True)

    entity_type: str = Field(max_length=64, index=True, nullable=False)
    entity_id: int = Field(index=True, nullable=False)
    action: AuditAction = Field(index=True, nullable=False)

    previous_state: Optional[dict] = Field(default=None, sa_column=Column(JSON, nullable=True))
    new_state: Optional[dict] = Field(default=None, sa_column=Column(JSON, nullable=True))
    description: Optional[str] = Field(default=None, max_length=500)
    created_at: int = Field(default_factory=utc_now_ts, index=True, nullable=False)
`;

export const DATABASE_PY_CODE = `"""
database.py — Async SQLite Database Engine & Session Configuration (aiosqlite + SQLModel).

Infrastructure Highlights:
1. Uses \`sqlite+aiosqlite\` for non-blocking async I/O in FastAPI.
2. Attaches a connection-level SQLAlchemy event listener on \`engine.sync_engine\` so that
   EVERY new SQLite connection automatically executes:
     - \`PRAGMA journal_mode=WAL;\`   (Write-Ahead Logging for concurrent reads + single writer)
     - \`PRAGMA foreign_keys=ON;\`    (Strict relational integrity for Parent-Child shifts & RBAC)
     - \`PRAGMA synchronous=NORMAL;\` (Safe & fast fsync behavior tailored for WAL mode)
     - \`PRAGMA busy_timeout=5000;\`  (5-second lock wait to prevent SQLITE_BUSY under swap bursts)
     - \`PRAGMA temp_store=MEMORY;\`  (In-memory temporary B-trees for analytical overlap queries)
3. Provides \`get_session()\` dependency for FastAPI routes and an \`atomic_transaction()\` helper
   used when committing \`APPROVED\` Shift Swaps alongside their \`AuditLog\` entries.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

import models  # noqa: F401

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./data/anesthesia_icu_scheduler.db",
)

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=os.getenv("SQL_ECHO", "false").lower() == "true",
    future=True,
    connect_args={
        "check_same_thread": False,
        "timeout": 15.0,
    },
)


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:
    """
    Executed automatically on every new SQLite physical connection.
    Enforces WAL mode for high concurrency and enables strict Foreign Key enforcement.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.execute("PRAGMA temp_store=MEMORY;")
    finally:
        cursor.close()


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
    """
    if DATABASE_URL.startswith("sqlite+aiosqlite:///"):
        raw_path = DATABASE_URL.replace("sqlite+aiosqlite:///", "", 1)
        db_dir = os.path.dirname(os.path.abspath(raw_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

        journal_mode = (await conn.execute(text("PRAGMA journal_mode;"))).scalar_one()
        fk_enabled = (await conn.execute(text("PRAGMA foreign_keys;"))).scalar_one()
        if str(journal_mode).lower() != "wal":
            raise RuntimeError(f"Expected SQLite WAL mode, got: {journal_mode}")
        if int(fk_enabled) != 1:
            raise RuntimeError("SQLite PRAGMA foreign_keys failed to enable.")


async def close_db() -> None:
    await engine.dispose()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
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
    """
    if session.in_transaction():
        async with session.begin_nested():
            yield session
        await session.commit()
    else:
        async with session.begin():
            yield session
`;
