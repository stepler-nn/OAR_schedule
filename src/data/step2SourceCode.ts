export const VALIDATION_PY_CODE = `"""
services/validation.py — Shift Validation, ICU Coverage & 32h Fatigue Calculation Engine.

Core Domain Responsibilities:
1. Time Overlap Check (\`StartA < EndB AND EndA > StartB\`):
   - Prevents a doctor from being assigned two overlapping primary (\`BASE_SHIFT\`) shifts
     across any hospital.
   - Prevents overlapping sibling \`OPERATIONAL_TASK\` assignments within the same parent shift.
   - Explicitly permits an \`OPERATIONAL_TASK\` (e.g., 08:00-16:00 OR #2) to overlap with its
     own parent \`BASE_SHIFT\` (e.g., 24h ICU duty at the same hospital).
2. Hospital Consistency & Temporal Containment:
   - Enforces that any child \`OPERATIONAL_TASK\` belongs to the exact same \`hospital_id\`
     as its parent 24h \`BASE_SHIFT\` (\`child.hospital_id == parent.hospital_id\`).
   - Enforces that the child task's \`[start_ts, end_ts]\` window is strictly contained within
     the parent shift's \`[start_ts, end_ts]\` window.
3. Hospital ICU Coverage Engine:
   - Inspects any UTC date/time range across all active hospitals requiring 24h ICU coverage
     and returns structured \`ICUCoverageGap\` alerts for any hospital/day missing a scheduled
     24h ICU duty doctor.
4. Consecutive Shift Stitching & 32-Hour Fatigue Calculation:
   - Stitches contiguous shifts (where \`ShiftA.end_ts == ShiftB.start_ts\`, or separated by less
     than \`MIN_REST_RESET_SECONDS\`) for a doctor.
   - If cumulative continuous work exceeds 24 hours (e.g., 24h ICU duty + 8h daytime OR = 32h),
     it flags the shift with \`FatigueLevel.FATIGUE_RISK_HIGH\` (\`is_fatigue_risk = True\`) and
     computes \`fatigue_risk_hours\` WITHOUT blocking schedule creation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    Hospital,
    Shift,
    ShiftStatus,
    ShiftType,
    Workplace,
    WorkplaceType,
)

# =====================================================================
# 1. CONSTANTS & CUSTOM DOMAIN EXCEPTIONS
# =====================================================================

SECONDS_PER_HOUR: int = 3600
SECONDS_PER_DAY: int = 86400

FATIGUE_THRESHOLD_HOURS: float = 24.0
FATIGUE_THRESHOLD_SECONDS: int = int(FATIGUE_THRESHOLD_HOURS * SECONDS_PER_HOUR)
MIN_REST_RESET_SECONDS: int = 2 * SECONDS_PER_HOUR


class FatigueLevel(str, Enum):
    """Analytical classification for continuous duty duration."""
    NOMINAL = "NOMINAL"                      # <= 24.0 continuous hours
    FATIGUE_RISK_HIGH = "FATIGUE_RISK_HIGH"  # > 24.0 continuous hours (e.g., 32h shift)


class ScheduleValidationError(Exception):
    """Base exception for all clinical scheduling constraint violations."""

    def __init__(self, message: str, code: str = "SCHEDULE_VALIDATION_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class TimeOverlapError(ScheduleValidationError):
    """Raised when a doctor has a conflicting shift (\`StartA < EndB AND EndA > StartB\`)."""

    def __init__(
        self,
        doctor_id: int,
        conflicting_shift_id: Optional[int],
        conflict_start_ts: int,
        conflict_end_ts: int,
        message: Optional[str] = None,
    ) -> None:
        msg = message or (
            f"Time overlap violation: Doctor #{doctor_id} is already scheduled on Shift "
            f"#{conflicting_shift_id} during [{conflict_start_ts}, {conflict_end_ts})."
        )
        super().__init__(msg, code="TIME_OVERLAP_VIOLATION")
        self.doctor_id = doctor_id
        self.conflicting_shift_id = conflicting_shift_id
        self.conflict_start_ts = conflict_start_ts
        self.conflict_end_ts = conflict_end_ts


class HospitalMismatchError(ScheduleValidationError):
    """
    Raised when a child Operational Task (e.g., OR #2) is assigned to a different hospital
    than its parent 24h Base Shift.
    """

    def __init__(self, child_hospital_id: int, parent_hospital_id: int, parent_shift_id: int) -> None:
        super().__init__(
            f"Physical restriction violation: Child operational task in hospital_id={child_hospital_id} "
            f"cannot be linked to Parent Base Shift #{parent_shift_id} in hospital_id={parent_hospital_id}.",
            code="HOSPITAL_CONSISTENCY_VIOLATION",
        )
        self.child_hospital_id = child_hospital_id
        self.parent_hospital_id = parent_hospital_id
        self.parent_shift_id = parent_shift_id


class TemporalContainmentError(ScheduleValidationError):
    """
    Raised when a child Operational Task's time interval falls outside its Parent Base Shift's window.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="TEMPORAL_CONTAINMENT_VIOLATION")


@dataclass(frozen=True)
class FatigueAssessment:
    fatigue_level: FatigueLevel
    is_fatigue_risk: bool
    continuous_hours_at_end: float
    fatigue_risk_hours: float
    chain_start_ts: int
    chain_end_ts: int
    stitched_shift_ids: List[int] = field(default_factory=list)


@dataclass(frozen=True)
class ShiftValidationResult:
    is_valid: bool
    fatigue: FatigueAssessment
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ICUCoverageGap:
    hospital_id: int
    hospital_code: str
    hospital_name: str
    day_iso: str
    window_start_ts: int
    window_end_ts: int
    covered_hours: float
    missing_hours: float
    unassigned_intervals: List[Tuple[int, int]]


class ShiftValidationService:
    """
    Stateless domain service encapsulating all pre-save schedule validations,
    32-hour continuous fatigue calculations, and multi-hospital 24h ICU coverage checks.
    """

    @classmethod
    async def validate_shift_before_save(
        cls,
        session: AsyncSession,
        *,
        doctor_id: int,
        hospital_id: int,
        workplace_id: int,
        start_ts: int,
        end_ts: int,
        shift_type: ShiftType = ShiftType.BASE_SHIFT,
        parent_shift_id: Optional[int] = None,
        exclude_shift_id: Optional[int] = None,
    ) -> ShiftValidationResult:
        if end_ts <= start_ts:
            raise ScheduleValidationError(
                f"Shift end_ts ({end_ts}) must be strictly greater than start_ts ({start_ts}).",
                code="INVALID_TIME_INTERVAL",
            )

        workplace = await session.get(Workplace, workplace_id)
        if workplace is None:
            raise ScheduleValidationError(
                f"Workplace #{workplace_id} does not exist.",
                code="WORKPLACE_NOT_FOUND",
            )
        if workplace.hospital_id != hospital_id:
            raise HospitalMismatchError(
                child_hospital_id=workplace.hospital_id,
                parent_hospital_id=hospital_id,
                parent_shift_id=parent_shift_id or 0,
            )

        if shift_type == ShiftType.BASE_SHIFT and parent_shift_id is not None:
            raise TemporalContainmentError("A BASE_SHIFT cannot have a parent_shift_id.")

        if shift_type == ShiftType.OPERATIONAL_TASK:
            if parent_shift_id is None:
                raise TemporalContainmentError(
                    "An OPERATIONAL_TASK must reference a valid parent_shift_id."
                )
            await cls._validate_child_task_against_parent(
                session=session,
                doctor_id=doctor_id,
                child_hospital_id=hospital_id,
                child_start_ts=start_ts,
                child_end_ts=end_ts,
                parent_shift_id=parent_shift_id,
            )

        await cls.check_time_overlap(
            session=session,
            doctor_id=doctor_id,
            start_ts=start_ts,
            end_ts=end_ts,
            shift_type=shift_type,
            parent_shift_id=parent_shift_id,
            exclude_shift_id=exclude_shift_id,
        )

        fatigue = await cls.calculate_doctor_fatigue(
            session=session,
            doctor_id=doctor_id,
            proposed_start_ts=start_ts,
            proposed_end_ts=end_ts,
            shift_type=shift_type,
            exclude_shift_id=exclude_shift_id,
        )

        warnings: List[str] = []
        if fatigue.fatigue_level == FatigueLevel.FATIGUE_RISK_HIGH:
            warnings.append(
                f"FATIGUE_RISK_HIGH: Doctor #{doctor_id} will reach "
                f"{fatigue.continuous_hours_at_end:.1f}h of continuous duty "
                f"({fatigue.fatigue_risk_hours:.1f}h above the 24h threshold)."
            )

        return ShiftValidationResult(is_valid=True, fatigue=fatigue, warnings=warnings)
`;

export const FSM_PY_CODE = `"""
services/fsm.py — Shift Swap & Schedule Request Finite State Machine (FSM) Service.

Domain Responsibilities:
1. Canonical FSM State Transition Graph (\`ALLOWED_STATE_TRANSITIONS\`):
   DRAFT -> PROPOSED -> PEER_ACCEPTED / PEER_REJECTED ->
   APPROVED / REJECTED_BY_MANAGER / REVISION_REQUESTED -> CANCELLED
2. Strict Role-Based Access Control (RBAC) Enforcement:
   - DOCTOR (Requester): Can propose (\`DRAFT -> PROPOSED\`), resubmit revisions
     (\`REVISION_REQUESTED -> PROPOSED\`), request revision on rejected requests, or \`CANCELLED\`.
   - DOCTOR (Target Peer): Can accept (\`PROPOSED -> PEER_ACCEPTED\`) or reject
     (\`PROPOSED -> PEER_REJECTED\`) a swap offer addressed to them.
   - SENIOR_RESIDENT (Hospital-Scoped Manager): Can adjudicate (\`APPROVED\`,
     \`REJECTED_BY_MANAGER\`, \`REVISION_REQUESTED\`) ONLY for requests where
     \`request.hospital_id == senior_resident.assigned_hospital_id\`.
   - HEAD_OF_DEPT (Global Manager): Can adjudicate requests across ALL hospitals.
3. Atomic Database Transaction on \`APPROVED\`:
   - Re-validates time overlaps and recalculates 32h \`FATIGUE_RISK_HIGH\` flags for both doctors.
   - Atomically swaps \`doctor_id\` on \`source_shift\` and \`target_shift\` (plus any linked child
     \`OPERATIONAL_TASK\` shifts so parent/child shifts remain bound to the same doctor).
   - Updates \`ShiftRequest.status = RequestStatus.APPROVED\`.
   - Writes an immutable \`AuditLog\` (\`AuditAction.ATOMIC_SWAP_COMMIT\`) in the same SQLite transaction.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import atomic_transaction
from models import (
    ALLOWED_FSM_TRANSITIONS,
    AuditAction,
    AuditLog,
    RequestStatus,
    RequestType,
    Shift,
    ShiftRequest,
    ShiftStatus,
    ShiftType,
    User,
    UserRole,
    utc_now_ts,
)
from services.validation import ScheduleValidationError, ShiftValidationService

ALLOWED_STATE_TRANSITIONS: Dict[RequestStatus, Set[RequestStatus]] = ALLOWED_FSM_TRANSITIONS


class FSMError(Exception):
    def __init__(self, message: str, code: str = "FSM_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class InvalidStateTransitionError(FSMError):
    def __init__(
        self,
        current_status: RequestStatus,
        target_status: RequestStatus,
        allowed: Set[RequestStatus],
    ) -> None:
        allowed_str = ", ".join(sorted(s.value for s in allowed)) or "NONE (terminal state)"
        super().__init__(
            f"Invalid FSM state transition: '{current_status.value}' -> '{target_status.value}'. "
            f"Allowed next states: [{allowed_str}].",
            code="INVALID_FSM_TRANSITION",
        )


class RBACPermissionDeniedError(FSMError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="RBAC_PERMISSION_DENIED")


class SwapConflictError(FSMError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="SWAP_COMMIT_CONFLICT")


class ShiftSwapFSMService:
    @classmethod
    async def transition_request_state(
        cls,
        session: AsyncSession,
        *,
        request_id: int,
        target_status: RequestStatus,
        actor: User,
        notes: Optional[str] = None,
    ) -> ShiftRequest:
        async with atomic_transaction(session):
            shift_request = await session.get(ShiftRequest, request_id)
            if shift_request is None:
                raise FSMError(f"ShiftRequest #{request_id} not found.", code="REQUEST_NOT_FOUND")

            previous_status = shift_request.status
            allowed_next = ALLOWED_STATE_TRANSITIONS.get(previous_status, set())
            if target_status not in allowed_next:
                raise InvalidStateTransitionError(
                    current_status=previous_status,
                    target_status=target_status,
                    allowed=allowed_next,
                )

            cls._authorize_transition(
                shift_request=shift_request,
                target_status=target_status,
                actor=actor,
            )

            now_ts = utc_now_ts()
            if target_status in {RequestStatus.PEER_ACCEPTED, RequestStatus.PEER_REJECTED}:
                shift_request.peer_response_note = notes
                shift_request.peer_responded_at = now_ts
            elif target_status in {
                RequestStatus.APPROVED,
                RequestStatus.REJECTED_BY_MANAGER,
                RequestStatus.REVISION_REQUESTED,
            } and actor.role in {UserRole.SENIOR_RESIDENT, UserRole.HEAD_OF_DEPT}:
                shift_request.reviewed_by_id = actor.id
                shift_request.manager_notes = notes
                shift_request.reviewed_at = now_ts

            swap_audit_payload: Optional[dict] = None
            if target_status == RequestStatus.APPROVED:
                swap_audit_payload = await cls._execute_atomic_shift_swap(
                    session=session,
                    shift_request=shift_request,
                    approver=actor,
                )

            shift_request.transition_to(target_status)
            session.add(shift_request)

            audit_action = (
                AuditAction.ATOMIC_SWAP_COMMIT
                if target_status == RequestStatus.APPROVED
                else AuditAction.FSM_TRANSITION
            )
            audit_entry = AuditLog(
                actor_id=actor.id,
                actor_role=actor.role,
                hospital_id=shift_request.hospital_id,
                entity_type="ShiftRequest",
                entity_id=shift_request.id or request_id,
                action=audit_action,
                previous_state={
                    "status": previous_status.value,
                    **(swap_audit_payload["before"] if swap_audit_payload else {}),
                },
                new_state={
                    "status": target_status.value,
                    **(swap_audit_payload["after"] if swap_audit_payload else {}),
                },
                description=(
                    f"Request #{request_id} transitioned {previous_status.value} -> "
                    f"{target_status.value} by {actor.full_name} ({actor.role.value})."
                ),
                created_at=now_ts,
            )
            session.add(audit_entry)
            await session.flush()

            return shift_request
`;
