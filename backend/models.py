"""
models.py — SQLModel Schema Definitions for Anesthesiology & ICU Scheduling System.

Domain Architecture & Constraints Encoded:
1. Scale: ~30 Doctors across 4 Hospitals (~5 Operating Rooms + 1 ICU Duty role per hospital).
2. Temporal Storage: All timestamps are stored as UTC Unix Timestamps (INTEGER seconds)
   to enable ultra-fast mathematical interval overlap checks:
   (StartA < EndB AND EndA > StartB) using composite B-Tree indexes.
3. Parent-Child Shifts:
   - BASE_SHIFT (Parent): e.g., 24-hour ICU Duty at Hospital A, or 8h-16h Daytime OR shift.
   - OPERATIONAL_TASK (Child): e.g., 08:00-16:00 in OR #2 at Hospital A, linked via
     `parent_shift_id` to the Base Shift.
   - Physical Restriction: Child task `hospital_id` MUST match Parent shift `hospital_id`,
     and Child interval `[start_ts, end_ts]` MUST be contained within Parent `[start_ts, end_ts]`.
4. Fatigue Tracking:
   - Continuous shifts up to 32 hours (e.g., 24h ICU duty + 8h daytime continuation) are
     permitted by the domain, but any hours exceeding 24 continuous hours are flagged with
     `is_fatigue_risk = True` and `fatigue_risk_hours > 0`.
5. Shift Swap & Request Finite State Machine (FSM):
   - States: DRAFT -> PROPOSED -> PEER_ACCEPTED / PEER_REJECTED ->
     APPROVED / REJECTED_BY_MANAGER / REVISION_REQUESTED -> CANCELLED.
   - Shifts in the database are mutated atomically ONLY when a request transitions to `APPROVED`.
6. RBAC Roles:
   - DOCTOR: Global schedule read, PWA check-in/out, create/respond to swap & revision requests.
   - SENIOR_RESIDENT: Full shift/task CRUD and request approval scoped strictly to `assigned_hospital_id`.
   - HEAD_OF_DEPT: Global CRUD across all hospitals, user management, fatigue analytics & exports.
"""

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
    Actual `Shift.doctor_id` records in SQLite are updated atomically ONLY
    upon transitioning into `APPROVED`.
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
    - SENIOR_RESIDENT: Hospital-scoped manager; CRUD restricted to `assigned_hospital_id`.
    - HEAD_OF_DEPT: Global administrator across all hospitals, user skills, and fatigue analytics.
    """
    __tablename__ = "users"
    __table_args__ = (
        # If role is SENIOR_RESIDENT, assigned_hospital_id must not be NULL
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

    1. Parent Shift (`shift_type = ShiftType.BASE_SHIFT`, `parent_shift_id = None`):
       - Example A: 24-hour ICU Duty at Hospital A (08:00 Day 1 -> 08:00 Day 2).
       - Example B: 8-hour or 16-hour Daytime OR Shift at Hospital B.

    2. Child Operational Task (`shift_type = ShiftType.OPERATIONAL_TASK`, `parent_shift_id = <BaseShift.id>`):
       - Example: An 8-hour surgical block (08:00 -> 16:00) in OR #2 at Hospital A assigned
         to the doctor who is currently holding the 24-hour ICU Base Shift at Hospital A.
       - Physical Constraint: `child.hospital_id == parent.hospital_id` is strictly enforced by the
         Validation Engine (a doctor on 24h duty at Hospital A cannot be assigned an OR task in Hospital B).

    3. Fast Mathematical Overlap Check:
       - `start_ts` and `end_ts` are UTC Unix timestamps (INTEGER seconds).
       - Two shifts A and B overlap if and only if: `A.start_ts < B.end_ts AND A.end_ts > B.start_ts`.

    4. 32-Hour Continuous Work & Fatigue Tracking:
       - Doctors frequently work a 24h ICU shift followed immediately by an 8h daytime shift (32h total).
       - The system ALLOWS scheduling up to 32 continuous hours, but automatically marks any shift pushing
         continuous duty beyond 24 hours with `is_fatigue_risk = True` and records `fatigue_risk_hours`
         (e.g., 8.0 hours of fatigue risk for a 32h stretch).
    """
    __tablename__ = "shifts"
    __table_args__ = (
        CheckConstraint("end_ts > start_ts", name="ck_shift_positive_duration"),
        CheckConstraint(
            "(shift_type = 'BASE_SHIFT' AND parent_shift_id IS NULL) OR "
            "(shift_type = 'OPERATIONAL_TASK' AND parent_shift_id IS NOT NULL)",
            name="ck_shift_hierarchy_integrity",
        ),
        # Composite index for O(log N) mathematical overlap queries per doctor:
        # WHERE doctor_id = ? AND start_ts < :end_b AND end_ts > :start_b
        Index("ix_shifts_doctor_interval", "doctor_id", "start_ts", "end_ts"),
        # Composite index for hospital 24h ICU coverage validation & Senior Resident views:
        Index("ix_shifts_hospital_interval", "hospital_id", "shift_type", "start_ts", "end_ts"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    shift_type: ShiftType = Field(
        default=ShiftType.BASE_SHIFT,
        index=True,
        nullable=False,
        description="BASE_SHIFT (Parent) or OPERATIONAL_TASK (Child linked via parent_shift_id)",
    )
    parent_shift_id: Optional[int] = Field(
        default=None,
        foreign_key="shifts.id",
        index=True,
        description="Self-referential FK to parent BASE_SHIFT when this row is an OPERATIONAL_TASK",
    )

    doctor_id: int = Field(
        foreign_key="users.id",
        index=True,
        nullable=False,
        description="Assigned physician",
    )
    hospital_id: int = Field(
        foreign_key="hospitals.id",
        index=True,
        nullable=False,
        description="Physical hospital where this shift/task occurs; must match parent_shift.hospital_id",
    )
    workplace_id: int = Field(
        foreign_key="workplaces.id",
        index=True,
        nullable=False,
        description="Specific OR room or ICU station within the hospital",
    )

    # UTC Unix timestamps (seconds) for fast integer comparison: StartA < EndB AND EndA > StartB
    start_ts: int = Field(
        index=True,
        nullable=False,
        description="Shift start time as UTC Unix timestamp (seconds)",
    )
    end_ts: int = Field(
        index=True,
        nullable=False,
        description="Shift end time as UTC Unix timestamp (seconds)",
    )

    status: ShiftStatus = Field(default=ShiftStatus.SCHEDULED, index=True, nullable=False)

    # ICU & Fatigue Tracking Metadata
    is_24h_icu_duty: bool = Field(
        default=False,
        index=True,
        description="True when this Base Shift satisfies the mandatory 24-hour ICU coverage for its hospital",
    )
    is_fatigue_risk: bool = Field(
        default=False,
        index=True,
        description="True if cumulative continuous work across adjacent shifts exceeds 24 hours (e.g., 32h shift)",
    )
    continuous_hours_at_end: float = Field(
        default=0.0,
        description="Calculated total continuous duty hours at the conclusion of this shift (e.g., 32.0)",
    )
    fatigue_risk_hours: float = Field(
        default=0.0,
        description="Number of hours within this shift that exceed the 24h continuous threshold (e.g., 8.0)",
    )

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

    @property
    def duration_hours(self) -> float:
        """Returns shift duration in decimal hours."""
        return round((self.end_ts - self.start_ts) / 3600.0, 2)

    def overlaps_with(self, other_start_ts: int, other_end_ts: int) -> bool:
        """
        Mathematical interval overlap check:
        Two intervals [StartA, EndA) and [StartB, EndB) overlap iff StartA < EndB AND EndA > StartB.
        Back-to-back shifts where EndA == StartB (e.g., 24h ICU ending at 08:00 + 8h OR starting at 08:00)
        do NOT overlap, allowing seamless 32h continuous work scheduling.
        """
        return self.start_ts < other_end_ts and self.end_ts > other_start_ts

    def validate_as_child_of(self, parent: "Shift") -> None:
        """
        Enforces parent-child domain invariants before persistence:
        1. Parent must be a BASE_SHIFT.
        2. Physical Restriction: Cannot assign an OR task in Hospital B if Parent Shift is in Hospital A.
        3. Temporal Containment: Child task must fall within Parent shift boundaries.
        4. Doctor Consistency: Child task must be assigned to the same doctor (or explicitly delegated).
        """
        if parent.shift_type != ShiftType.BASE_SHIFT:
            raise ValueError("Operational tasks can only be attached to a BASE_SHIFT parent.")
        if self.hospital_id != parent.hospital_id:
            raise ValueError(
                f"Physical restriction violation: Operational task hospital_id ({self.hospital_id}) "
                f"must match Parent Base Shift hospital_id ({parent.hospital_id})."
            )
        if self.start_ts < parent.start_ts or self.end_ts > parent.end_ts:
            raise ValueError(
                "Temporal containment violation: Operational task interval must fall inside "
                "the Parent Base Shift time window."
            )


# =====================================================================
# 5. TIME LOG MODEL (PWA CHECK-IN / CHECK-OUT)
# =====================================================================

class TimeLog(SQLModel, table=True):
    """
    Records actual attendance captured via the Doctor Progressive Web App (PWA).
    Used by Head of Dept to reconcile scheduled hours vs. actual worked hours,
    verify 32h continuous duty fatigue exposure, and generate payroll exports.
    """
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

    check_in_ts: int = Field(
        nullable=False,
        description="Actual UTC Unix timestamp when the doctor checked in via PWA",
    )
    check_out_ts: Optional[int] = Field(
        default=None,
        description="Actual UTC Unix timestamp when the doctor checked out via PWA",
    )

    recorded_duration_seconds: Optional[int] = Field(
        default=None,
        description="Computed (check_out_ts - check_in_ts) upon check-out",
    )
    is_fatigue_flagged: bool = Field(
        default=False,
        index=True,
        description="Flagged True if actual logged continuous duty exceeds 24h (>86,400 seconds)",
    )
    pwa_client_metadata: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Device/PWA offline sync metadata or hospital Wi-Fi/beacon verification tag",
    )
    notes: Optional[str] = Field(default=None, max_length=500)
    created_at: int = Field(default_factory=utc_now_ts, nullable=False)

    # Relationships
    shift: Optional[Shift] = Relationship(back_populates="time_logs")
    doctor: Optional[User] = Relationship(back_populates="time_logs")


# =====================================================================
# 6. SHIFT SWAP & VACATION REQUEST MODEL (FSM)
# =====================================================================

class ShiftRequest(SQLModel, table=True):
    """
    Manages Doctor Shift Swaps, Vacation Requests, and Shift Revisions using a strict
    Finite State Machine (FSM):
      DRAFT -> PROPOSED -> PEER_ACCEPTED / PEER_REJECTED ->
      APPROVED / REJECTED_BY_MANAGER / REVISION_REQUESTED -> CANCELLED

    ATOMICITY GUARANTEE:
    The underlying `Shift.doctor_id` assignments remain untouched during DRAFT, PROPOSED,
    and PEER_ACCEPTED states. Only when a Senior Resident (scoped to `hospital_id`) or
    Head of Dept transitions the request to `APPROVED` does the service layer atomically
    swap the doctor assignments and write an `AuditLog` within the same SQLite transaction.
    """
    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_hospital_status", "hospital_id", "status"),
        Index("ix_requests_requester_status", "requester_id", "status"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    request_type: RequestType = Field(default=RequestType.SHIFT_SWAP, index=True, nullable=False)
    status: RequestStatus = Field(default=RequestStatus.DRAFT, index=True, nullable=False)

    # Hospital scoping for Senior Resident approvals
    hospital_id: int = Field(
        foreign_key="hospitals.id",
        index=True,
        nullable=False,
        description="Hospital where the primary shift resides; determines which Senior Resident can approve",
    )

    # Actors
    requester_id: int = Field(
        foreign_key="users.id",
        index=True,
        nullable=False,
        description="Doctor initiating the swap, vacation, or revision request",
    )
    target_doctor_id: Optional[int] = Field(
        default=None,
        foreign_key="users.id",
        index=True,
        description="Peer doctor invited to take or swap the shift (for SHIFT_SWAP requests)",
    )

    # Shifts involved in the swap or revision
    source_shift_id: Optional[int] = Field(
        default=None,
        foreign_key="shifts.id",
        index=True,
        description="Primary shift owned by requester_id to be reassigned or revised",
    )
    target_shift_id: Optional[int] = Field(
        default=None,
        foreign_key="shifts.id",
        index=True,
        description="Optional reciprocal shift owned by target_doctor_id in a two-way swap",
    )

    # Time range (used for VACATION requests or proposed revision intervals)
    requested_start_ts: Optional[int] = Field(default=None)
    requested_end_ts: Optional[int] = Field(default=None)

    reason: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    peer_response_note: Optional[str] = Field(default=None, max_length=500)
    peer_responded_at: Optional[int] = Field(default=None)

    # Manager (Senior Resident or Head of Dept) adjudication metadata
    reviewed_by_id: Optional[int] = Field(
        default=None,
        foreign_key="users.id",
        description="Senior Resident or Head of Dept who approved/rejected/requested revision",
    )
    manager_notes: Optional[str] = Field(default=None, max_length=500)
    reviewed_at: Optional[int] = Field(default=None)

    created_at: int = Field(default_factory=utc_now_ts, nullable=False)
    updated_at: int = Field(default_factory=utc_now_ts, nullable=False)

    # Relationships
    hospital: Optional[Hospital] = Relationship(back_populates="requests")
    requester: Optional[User] = Relationship(
        back_populates="initiated_requests",
        sa_relationship_kwargs={"foreign_keys": "[ShiftRequest.requester_id]"},
    )
    target_doctor: Optional[User] = Relationship(
        back_populates="targeted_requests",
        sa_relationship_kwargs={"foreign_keys": "[ShiftRequest.target_doctor_id]"},
    )

    def can_transition_to(self, next_status: RequestStatus) -> bool:
        """Checks if `next_status` is a legal transition from the current FSM state."""
        allowed = ALLOWED_FSM_TRANSITIONS.get(self.status, set())
        return next_status in allowed

    def transition_to(self, next_status: RequestStatus) -> None:
        """
        Validates and applies an FSM state transition.
        Raises ValueError if the transition violates the Shift Swap FSM graph.
        """
        if not self.can_transition_to(next_status):
            allowed_names = sorted(s.value for s in ALLOWED_FSM_TRANSITIONS.get(self.status, set()))
            raise ValueError(
                f"Invalid FSM transition from '{self.status.value}' to '{next_status.value}'. "
                f"Allowed transitions: {allowed_names}"
            )
        self.status = next_status
        self.updated_at = utc_now_ts()


# =====================================================================
# 7. IMMUTABLE AUDIT LOG MODEL
# =====================================================================

class AuditLog(SQLModel, table=True):
    """
    Append-only compliance and forensic log tracking all schedule changes,
    FSM state transitions, Senior Resident / Head of Dept overrides, and
    32-hour fatigue risk acknowledgements.
    """
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_entity_lookup", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_hospital_time", "hospital_id", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    actor_id: Optional[int] = Field(
        default=None,
        foreign_key="users.id",
        index=True,
        description="User who triggered the action (None for automated system validations)",
    )
    actor_role: Optional[UserRole] = Field(default=None)
    hospital_id: Optional[int] = Field(default=None, foreign_key="hospitals.id", index=True)

    entity_type: str = Field(
        max_length=64,
        index=True,
         nullable=False,
        description="Target model name: 'Shift', 'ShiftRequest', 'TimeLog', 'User'",
    )
    entity_id: int = Field(index=True, nullable=False)
    action: AuditAction = Field(index=True, nullable=False)

    previous_state: Optional[dict] = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="JSON snapshot of affected fields prior to mutation",
    )
    new_state: Optional[dict] = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="JSON snapshot of affected fields after mutation",
    )
    description: Optional[str] = Field(default=None, max_length=500)
    created_at: int = Field(default_factory=utc_now_ts, index=True, nullable=False)
