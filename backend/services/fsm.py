"""
services/fsm.py — Shift Swap & Schedule Request Finite State Machine (FSM) Service.

Domain Responsibilities:
1. Canonical FSM State Transition Graph (`ALLOWED_STATE_TRANSITIONS`):
   DRAFT -> PROPOSED -> PEER_ACCEPTED / PEER_REJECTED ->
   APPROVED / REJECTED_BY_MANAGER / REVISION_REQUESTED -> CANCELLED
2. Strict Role-Based Access Control (RBAC) Enforcement:
   - DOCTOR (Requester): Can propose (`DRAFT -> PROPOSED`), resubmit revisions
     (`REVISION_REQUESTED -> PROPOSED`), request revision on rejected requests, or `CANCELLED`.
   - DOCTOR (Target Peer): Can accept (`PROPOSED -> PEER_ACCEPTED`) or reject
     (`PROPOSED -> PEER_REJECTED`) a swap offer addressed to them.
   - SENIOR_RESIDENT (Hospital-Scoped Manager): Can adjudicate (`APPROVED`,
     `REJECTED_BY_MANAGER`, `REVISION_REQUESTED`) ONLY for requests where
     `request.hospital_id == senior_resident.assigned_hospital_id`.
   - HEAD_OF_DEPT (Global Manager): Can adjudicate requests across ALL hospitals.
3. Atomic Database Transaction on `APPROVED`:
   - Re-validates time overlaps and recalculates 32h `FATIGUE_RISK_HIGH` flags for both doctors.
   - Atomically swaps `doctor_id` on `source_shift` and `target_shift` (plus any linked child
     `OPERATIONAL_TASK` shifts so parent/child shifts remain bound to the same doctor).
   - Updates `ShiftRequest.status = RequestStatus.APPROVED`.
   - Writes an immutable `AuditLog` (`AuditAction.ATOMIC_SWAP_COMMIT`) in the same SQLite transaction.
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

# =====================================================================
# 1. FSM TRANSITION MAPPING & CUSTOM EXCEPTIONS
# =====================================================================

# Exported transition dictionary matching the specification
ALLOWED_STATE_TRANSITIONS: Dict[RequestStatus, Set[RequestStatus]] = ALLOWED_FSM_TRANSITIONS


class FSMError(Exception):
    """Base exception for Shift Swap Finite State Machine errors."""

    def __init__(self, message: str, code: str = "FSM_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class InvalidStateTransitionError(FSMError):
    """Raised when attempting an illegal state transition in the Shift Swap FSM."""

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
        self.current_status = current_status
        self.target_status = target_status
        self.allowed = allowed


class RBACPermissionDeniedError(FSMError):
    """
    Raised when the acting user lacks role permissions or hospital scope
    to perform the requested FSM transition.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="RBAC_PERMISSION_DENIED")


class SwapConflictError(FSMError):
    """
    Raised when an atomic swap commit fails pre-commit validation (e.g., a doctor
    acquired a conflicting shift after the swap was originally proposed).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="SWAP_COMMIT_CONFLICT")


# =====================================================================
# 2. SHIFT SWAP STATE MACHINE SERVICE
# =====================================================================

class ShiftSwapFSMService:
    """
    Coordinates state transitions, RBAC authorization, and atomic SQLite commits
    for `ShiftRequest` entities.
    """

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
        """
        Transitions a `ShiftRequest` to `target_status` with strict FSM graph validation,
        RBAC verification, and—if `target_status == RequestStatus.APPROVED`—an atomic
        database swap of the underlying shifts and child operational tasks.
        """
        async with atomic_transaction(session):
            shift_request = await session.get(ShiftRequest, request_id)
            if shift_request is None:
                raise FSMError(f"ShiftRequest #{request_id} not found.", code="REQUEST_NOT_FOUND")

            previous_status = shift_request.status

            # 1. Verify state graph transition legality
            allowed_next = ALLOWED_STATE_TRANSITIONS.get(previous_status, set())
            if target_status not in allowed_next:
                raise InvalidStateTransitionError(
                    current_status=previous_status,
                    target_status=target_status,
                    allowed=allowed_next,
                )

            # 2. Enforce RBAC & Hospital Scoping Rules
            cls._authorize_transition(
                shift_request=shift_request,
                target_status=target_status,
                actor=actor,
            )

            now_ts = utc_now_ts()

            # 3. Record actor-specific metadata on the request
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
            elif notes:
                shift_request.reason = notes

            # 4. If reaching APPROVED, atomically mutate the Shift assignments in SQLite
            swap_audit_payload: Optional[dict] = None
            if target_status == RequestStatus.APPROVED:
                swap_audit_payload = await cls._execute_atomic_shift_swap(
                    session=session,
                    shift_request=shift_request,
                    approver=actor,
                )

            # 5. Apply the FSM state transition
            shift_request.transition_to(target_status)
            session.add(shift_request)

            # 6. Write immutable AuditLog entry within the same transaction
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

    @classmethod
    def _authorize_transition(
        cls,
        *,
        shift_request: ShiftRequest,
        target_status: RequestStatus,
        actor: User,
    ) -> None:
        """
        Enforces Role-Based Access Control (RBAC) for each FSM state transition:
        - `PROPOSED`: Only the initiating doctor (`requester_id`) can submit or resubmit.
        - `PEER_ACCEPTED` / `PEER_REJECTED`: Only the invited peer (`target_doctor_id`)
          can accept or reject a swap proposal.
        - `APPROVED` / `REJECTED_BY_MANAGER`:
            * `DOCTOR` is strictly forbidden.
            * `SENIOR_RESIDENT` is permitted ONLY if `actor.assigned_hospital_id == shift_request.hospital_id`.
            * `HEAD_OF_DEPT` is permitted globally across all hospitals.
        - `REVISION_REQUESTED`:
            * Manager (`SENIOR_RESIDENT` for their hospital, or `HEAD_OF_DEPT` globally) can
              request revisions on `PROPOSED` or `PEER_ACCEPTED` requests.
            * Initiating doctor (`requester_id`) can transition `PEER_REJECTED` or
              `REJECTED_BY_MANAGER` into `REVISION_REQUESTED` to amend their request.
        - `CANCELLED`:
            * Initiating doctor (`requester_id`) or manager can cancel a non-terminal request.
        """
        is_requester = actor.id is not None and actor.id == shift_request.requester_id
        is_target_peer = (
            actor.id is not None
            and shift_request.target_doctor_id is not None
            and actor.id == shift_request.target_doctor_id
        )
        is_scoped_senior_resident = (
            actor.role == UserRole.SENIOR_RESIDENT
            and actor.assigned_hospital_id is not None
            and actor.assigned_hospital_id == shift_request.hospital_id
        )
        is_head_of_dept = actor.role == UserRole.HEAD_OF_DEPT
        is_authorized_manager = is_scoped_senior_resident or is_head_of_dept

        # 1. Submitting / Resubmitting to PROPOSED
        if target_status == RequestStatus.PROPOSED:
            if not is_requester:
                raise RBACPermissionDeniedError(
                    "Only the initiating doctor (requester) can transition a request to PROPOSED."
                )
            return

        # 2. Peer Acceptance or Rejection (Doctor B)
        if target_status in {RequestStatus.PEER_ACCEPTED, RequestStatus.PEER_REJECTED}:
            if shift_request.request_type == RequestType.SHIFT_SWAP:
                if not is_target_peer:
                    raise RBACPermissionDeniedError(
                        "Only the designated target peer doctor can accept or reject a SHIFT_SWAP."
                    )
                if shift_request.target_doctor_id == shift_request.requester_id:
                    raise RBACPermissionDeniedError(
                        "A doctor cannot peer-accept their own shift swap request."
                    )
                return
            # For non-swap requests (e.g., VACATION), allow an authorized manager to advance if needed
            if not is_authorized_manager:
                raise RBACPermissionDeniedError(
                    "Unauthorized to advance non-swap request to peer state."
                )
            return

        # 3. Manager Final Approval or Manager Rejection
        if target_status in {RequestStatus.APPROVED, RequestStatus.REJECTED_BY_MANAGER}:
            if actor.role == UserRole.DOCTOR:
                raise RBACPermissionDeniedError(
                    f"Role DOCTOR cannot transition a request to '{target_status.value}'. "
                    "Requires Senior Resident or Head of Department approval."
                )
            if actor.role == UserRole.SENIOR_RESIDENT and not is_scoped_senior_resident:
                raise RBACPermissionDeniedError(
                    f"Hospital scope violation: Senior Resident assigned to hospital_id="
                    f"{actor.assigned_hospital_id} has read-only access to hospital_id="
                    f"{shift_request.hospital_id} and cannot approve/reject its requests."
                )
            return

        # 4. Revision Requested
        if target_status == RequestStatus.REVISION_REQUESTED:
            if shift_request.status in {
                RequestStatus.PEER_REJECTED,
                RequestStatus.REJECTED_BY_MANAGER,
            }:
                if not (is_requester or is_authorized_manager):
                    raise RBACPermissionDeniedError(
                        "Only the requester or an authorized manager can initiate a revision after rejection."
                    )
                return
            # From PROPOSED or PEER_ACCEPTED, a manager (or requester) can flag REVISION_REQUESTED
            if actor.role == UserRole.SENIOR_RESIDENT and not is_scoped_senior_resident:
                raise RBACPermissionDeniedError(
                    f"Hospital scope violation: Senior Resident for hospital_id="
                    f"{actor.assigned_hospital_id} cannot request revisions in hospital_id="
                    f"{shift_request.hospital_id}."
                )
            if not (is_authorized_manager or is_requester):
                raise RBACPermissionDeniedError(
                    "Only the requester or an authorized hospital manager can request revisions."
                )
            return

        # 5. Cancellation
        if target_status == RequestStatus.CANCELLED:
            if not (is_requester or is_authorized_manager):
                raise RBACPermissionDeniedError(
                    "Only the initiating doctor or an authorized hospital manager can cancel a request."
                )
            return

    @classmethod
    async def _execute_atomic_shift_swap(
        cls,
        session: AsyncSession,
        *,
        shift_request: ShiftRequest,
        approver: User,
    ) -> dict:
        """
        Executes the atomic shift ownership mutation when a `ShiftRequest` reaches `APPROVED`.

        Supports:
        1. Two-way `SHIFT_SWAP` (`source_shift_id` owned by Doctor A <-> `target_shift_id` owned by Doctor B).
        2. One-way `SHIFT_SWAP` / giveaway (`source_shift_id` owned by Doctor A -> transferred to Doctor B).
        3. `REVISION` (`source_shift_id` updated to `requested_start_ts` / `requested_end_ts`).
        4. `VACATION` (`source_shift_id` if attached is marked `CANCELLED`).

        Crucially:
        - Cascades `doctor_id` updates to all child `OPERATIONAL_TASK` rows attached to the swapped
          Base Shifts so child OR tasks never become orphaned under the previous doctor.
        - Re-runs `ShiftValidationService.check_time_overlap` and `calculate_doctor_fatigue`
          for both doctors and updates `is_fatigue_risk`, `continuous_hours_at_end`, and
          `fatigue_risk_hours` on the mutated shifts.
        """
        now_ts = utc_now_ts()

        # Case A: SHIFT_SWAP
        if shift_request.request_type == RequestType.SHIFT_SWAP:
            if shift_request.source_shift_id is None or shift_request.target_doctor_id is None:
                raise SwapConflictError(
                    "SHIFT_SWAP request requires both source_shift_id and target_doctor_id."
                )

            source_shift = await session.get(Shift, shift_request.source_shift_id)
            if source_shift is None or source_shift.status == ShiftStatus.CANCELLED:
                raise SwapConflictError(
                    f"Source Shift #{shift_request.source_shift_id} is missing or cancelled."
                )

            if source_shift.doctor_id != shift_request.requester_id:
                raise SwapConflictError(
                    f"Source Shift #{source_shift.id} is no longer owned by requester "
                    f"#{shift_request.requester_id} (current owner: #{source_shift.doctor_id})."
                )

            doctor_a_id = shift_request.requester_id
            doctor_b_id = shift_request.target_doctor_id

            # Two-way swap if target_shift_id is provided
            if shift_request.target_shift_id is not None:
                target_shift = await session.get(Shift, shift_request.target_shift_id)
                if target_shift is None or target_shift.status == ShiftStatus.CANCELLED:
                    raise SwapConflictError(
                        f"Target Shift #{shift_request.target_shift_id} is missing or cancelled."
                    )
                if target_shift.doctor_id != doctor_b_id:
                    raise SwapConflictError(
                        f"Target Shift #{target_shift.id} is no longer owned by target peer "
                        f"#{doctor_b_id} (current owner: #{target_shift.doctor_id})."
                    )

                # Senior Resident cross-hospital guard on reciprocal shift
                if (
                    approver.role == UserRole.SENIOR_RESIDENT
                    and target_shift.hospital_id != approver.assigned_hospital_id
                ):
                    raise RBACPermissionDeniedError(
                        f"Reciprocal Shift #{target_shift.id} belongs to hospital_id="
                        f"{target_shift.hospital_id}. Cross-hospital two-way swaps require "
                        "HEAD_OF_DEPT approval."
                    )

                before_snapshot = {
                    "source_shift": {"id": source_shift.id, "doctor_id": source_shift.doctor_id},
                    "target_shift": {"id": target_shift.id, "doctor_id": target_shift.doctor_id},
                }

                # Validate Doctor B taking source_shift (excluding target_shift which Doctor B is vacating)
                try:
                    await ShiftValidationService.check_time_overlap(
                        session=session,
                        doctor_id=doctor_b_id,
                        start_ts=source_shift.start_ts,
                        end_ts=source_shift.end_ts,
                        shift_type=source_shift.shift_type,
                        parent_shift_id=source_shift.parent_shift_id,
                        exclude_shift_id=target_shift.id,
                    )
                    # Validate Doctor A taking target_shift (excluding source_shift which Doctor A is vacating)
                    await ShiftValidationService.check_time_overlap(
                        session=session,
                        doctor_id=doctor_a_id,
                        start_ts=target_shift.start_ts,
                        end_ts=target_shift.end_ts,
                        shift_type=target_shift.shift_type,
                        parent_shift_id=target_shift.parent_shift_id,
                        exclude_shift_id=source_shift.id,
                    )
                except ScheduleValidationError as exc:
                    raise SwapConflictError(str(exc)) from exc

                # Swap doctor_id on both primary shifts
                source_shift.doctor_id = doctor_b_id
                source_shift.updated_at = now_ts
                target_shift.doctor_id = doctor_a_id
                target_shift.updated_at = now_ts

                # Cascade ownership swap to any child OPERATIONAL_TASK shifts
                source_children = await cls._cascade_child_tasks_doctor(
                    session, parent_shift_id=source_shift.id, new_doctor_id=doctor_b_id, now_ts=now_ts
                )
                target_children = await cls._cascade_child_tasks_doctor(
                    session, parent_shift_id=target_shift.id, new_doctor_id=doctor_a_id, now_ts=now_ts
                )

                await session.flush()

                # Recalculate 32h fatigue metrics for both swapped shifts
                fatigue_b = await ShiftValidationService.calculate_doctor_fatigue(
                    session=session,
                    doctor_id=doctor_b_id,
                    proposed_start_ts=source_shift.start_ts,
                    proposed_end_ts=source_shift.end_ts,
                    shift_type=source_shift.shift_type,
                    exclude_shift_id=source_shift.id,
                )
                source_shift.is_fatigue_risk = fatigue_b.is_fatigue_risk
                source_shift.continuous_hours_at_end = fatigue_b.continuous_hours_at_end
                source_shift.fatigue_risk_hours = fatigue_b.fatigue_risk_hours

                fatigue_a = await ShiftValidationService.calculate_doctor_fatigue(
                    session=session,
                    doctor_id=doctor_a_id,
                    proposed_start_ts=target_shift.start_ts,
                    proposed_end_ts=target_shift.end_ts,
                    shift_type=target_shift.shift_type,
                    exclude_shift_id=target_shift.id,
                )
                target_shift.is_fatigue_risk = fatigue_a.is_fatigue_risk
                target_shift.continuous_hours_at_end = fatigue_a.continuous_hours_at_end
                target_shift.fatigue_risk_hours = fatigue_a.fatigue_risk_hours

                session.add(source_shift)
                session.add(target_shift)

                return {
                    "before": before_snapshot,
                    "after": {
                        "source_shift": {
                            "id": source_shift.id,
                            "doctor_id": source_shift.doctor_id,
                            "cascaded_child_task_ids": source_children,
                            "fatigue_level": fatigue_b.fatigue_level.value,
                            "continuous_hours_at_end": fatigue_b.continuous_hours_at_end,
                        },
                        "target_shift": {
                            "id": target_shift.id,
                            "doctor_id": target_shift.doctor_id,
                            "cascaded_child_task_ids": target_children,
                            "fatigue_level": fatigue_a.fatigue_level.value,
                            "continuous_hours_at_end": fatigue_a.continuous_hours_at_end,
                        },
                    },
                }

            # One-way shift transfer: Doctor A -> Doctor B
            before_snapshot = {
                "source_shift": {"id": source_shift.id, "doctor_id": source_shift.doctor_id}
            }

            try:
                await ShiftValidationService.check_time_overlap(
                    session=session,
                    doctor_id=doctor_b_id,
                    start_ts=source_shift.start_ts,
                    end_ts=source_shift.end_ts,
                    shift_type=source_shift.shift_type,
                    parent_shift_id=source_shift.parent_shift_id,
                    exclude_shift_id=source_shift.id,
                )
            except ScheduleValidationError as exc:
                raise SwapConflictError(str(exc)) from exc

            source_shift.doctor_id = doctor_b_id
            source_shift.updated_at = now_ts
            cascaded_children = await cls._cascade_child_tasks_doctor(
                session, parent_shift_id=source_shift.id, new_doctor_id=doctor_b_id, now_ts=now_ts
            )

            await session.flush()

            fatigue_b = await ShiftValidationService.calculate_doctor_fatigue(
                session=session,
                doctor_id=doctor_b_id,
                proposed_start_ts=source_shift.start_ts,
                proposed_end_ts=source_shift.end_ts,
                shift_type=source_shift.shift_type,
                exclude_shift_id=source_shift.id,
            )
            source_shift.is_fatigue_risk = fatigue_b.is_fatigue_risk
            source_shift.continuous_hours_at_end = fatigue_b.continuous_hours_at_end
            source_shift.fatigue_risk_hours = fatigue_b.fatigue_risk_hours
            session.add(source_shift)

            return {
                "before": before_snapshot,
                "after": {
                    "source_shift": {
                        "id": source_shift.id,
                        "doctor_id": source_shift.doctor_id,
                        "cascaded_child_task_ids": cascaded_children,
                        "fatigue_level": fatigue_b.fatigue_level.value,
                        "continuous_hours_at_end": fatigue_b.continuous_hours_at_end,
                    }
                },
            }

        # Case B: REVISION request (adjusts shift timestamps upon manager approval)
        if shift_request.request_type == RequestType.REVISION and shift_request.source_shift_id:
            source_shift = await session.get(Shift, shift_request.source_shift_id)
            if source_shift is None:
                raise SwapConflictError(f"Source Shift #{shift_request.source_shift_id} not found.")

            prev_window = {"start_ts": source_shift.start_ts, "end_ts": source_shift.end_ts}
            if shift_request.requested_start_ts and shift_request.requested_end_ts:
                try:
                    val_res = await ShiftValidationService.validate_shift_before_save(
                        session=session,
                        doctor_id=source_shift.doctor_id,
                        hospital_id=source_shift.hospital_id,
                        workplace_id=source_shift.workplace_id,
                        start_ts=shift_request.requested_start_ts,
                        end_ts=shift_request.requested_end_ts,
                        shift_type=source_shift.shift_type,
                        parent_shift_id=source_shift.parent_shift_id,
                        exclude_shift_id=source_shift.id,
                    )
                except ScheduleValidationError as exc:
                    raise SwapConflictError(str(exc)) from exc

                source_shift.start_ts = shift_request.requested_start_ts
                source_shift.end_ts = shift_request.requested_end_ts
                source_shift.is_fatigue_risk = val_res.fatigue.is_fatigue_risk
                source_shift.continuous_hours_at_end = val_res.fatigue.continuous_hours_at_end
                source_shift.fatigue_risk_hours = val_res.fatigue.fatigue_risk_hours
                source_shift.updated_at = now_ts
                session.add(source_shift)

            return {
                "before": {"source_shift": prev_window},
                "after": {
                    "source_shift": {
                        "start_ts": source_shift.start_ts,
                        "end_ts": source_shift.end_ts,
                    }
                },
            }

        # Case C: VACATION request
        return {"before": {}, "after": {"vacation_approved": True}}

    @staticmethod
    async def _cascade_child_tasks_doctor(
        session: AsyncSession,
        *,
        parent_shift_id: Optional[int],
        new_doctor_id: int,
        now_ts: int,
    ) -> List[int]:
        """
        Updates `doctor_id` on all active child `OPERATIONAL_TASK` rows attached to
        `parent_shift_id` so child OR tasks remain owned by the parent shift's doctor.
        """
        if parent_shift_id is None:
            return []

        stmt = select(Shift).where(
            and_(
                Shift.parent_shift_id == parent_shift_id,
                Shift.shift_type == ShiftType.OPERATIONAL_TASK,
                Shift.status != ShiftStatus.CANCELLED,
            )
        )
        result = await session.execute(stmt)
        children = result.scalars().all()

        updated_ids: List[int] = []
        for child in children:
            child.doctor_id = new_doctor_id
            child.updated_at = now_ts
            session.add(child)
            if child.id is not None:
                updated_ids.append(child.id)

        return updated_ids
