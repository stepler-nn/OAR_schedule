"""
api/routes/shifts.py — CRUD Endpoints for Parent Base Shifts & Child Operational OR Tasks.

Domain & RBAC Rules Enforced:
1. Read Access (`GET /api/v1/shifts`):
   - All authenticated roles (`DOCTOR`, `SENIOR_RESIDENT`, `HEAD_OF_DEPT`) can view schedules
     across all hospitals so doctors and managers have complete situational awareness.
2. Write Access (`POST`, `PATCH`, `DELETE`):
   - `SENIOR_RESIDENT`: Strictly scoped to `current_user.assigned_hospital_id == shift.hospital_id`
     via `verify_hospital_access()`.
   - `HEAD_OF_DEPT`: Global CRUD across all hospitals.
   - `DOCTOR`: Forbidden from direct schedule mutation (must use ShiftRequest FSM).
3. Validation Integration:
   - Calls `ShiftValidationService.validate_shift_before_save()` before every insert/update
     to enforce `StartA < EndB AND EndA > StartB` overlap checks, Parent-Child hospital
     consistency, and non-blocking 32-hour `FATIGUE_RISK_HIGH` tagging.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, verify_hospital_access
from database import atomic_transaction, get_session
from models import (
    AuditAction,
    AuditLog,
    Shift,
    ShiftStatus,
    ShiftType,
    User,
    utc_now_ts,
)
from services.validation import (
    FatigueLevel,
    HospitalMismatchError,
    ScheduleValidationError,
    ShiftValidationService,
    TemporalContainmentError,
    TimeOverlapError,
)

router = APIRouter(prefix="/shifts", tags=["Shifts & Operational Tasks"])


# =====================================================================
# 1. PYDANTIC REQUEST & RESPONSE SCHEMAS
# =====================================================================

class ShiftCreateRequest(BaseModel):
    """Payload for scheduling a Parent BASE_SHIFT or Child OPERATIONAL_TASK."""
    shift_type: ShiftType = Field(default=ShiftType.BASE_SHIFT)
    parent_shift_id: Optional[int] = Field(
        default=None,
        description="Required if shift_type == OPERATIONAL_TASK; must belong to same hospital_id",
    )
    doctor_id: int
    hospital_id: int
    workplace_id: int
    start_ts: int = Field(..., description="UTC Unix epoch start timestamp (seconds)")
    end_ts: int = Field(..., description="UTC Unix epoch end timestamp (seconds)")
    is_24h_icu_duty: bool = Field(default=False)
    notes: Optional[str] = Field(default=None, max_length=500)


class ShiftUpdateRequest(BaseModel):
    """Partial update payload for modifying an existing shift or task."""
    doctor_id: Optional[int] = None
    workplace_id: Optional[int] = None
    start_ts: Optional[int] = None
    end_ts: Optional[int] = None
    is_24h_icu_duty: Optional[bool] = None
    status: Optional[ShiftStatus] = None
    notes: Optional[str] = Field(default=None, max_length=500)


class ShiftResponse(BaseModel):
    """Serialized Shift or Operational Task with fatigue telemetry."""
    id: int
    shift_type: ShiftType
    parent_shift_id: Optional[int]
    doctor_id: int
    hospital_id: int
    workplace_id: int
    start_ts: int
    end_ts: int
    duration_hours: float
    status: ShiftStatus
    is_24h_icu_duty: bool
    is_fatigue_risk: bool
    fatigue_level: FatigueLevel
    continuous_hours_at_end: float
    fatigue_risk_hours: float
    notes: Optional[str]
    warnings: List[str] = Field(default_factory=list)


def _to_shift_response(shift: Shift, warnings: Optional[List[str]] = None) -> ShiftResponse:
    fatigue_level = (
        FatigueLevel.FATIGUE_RISK_HIGH if shift.is_fatigue_risk else FatigueLevel.NOMINAL
    )
    return ShiftResponse(
        id=shift.id or 0,
        shift_type=shift.shift_type,
        parent_shift_id=shift.parent_shift_id,
        doctor_id=shift.doctor_id,
        hospital_id=shift.hospital_id,
        workplace_id=shift.workplace_id,
        start_ts=shift.start_ts,
        end_ts=shift.end_ts,
        duration_hours=shift.duration_hours,
        status=shift.status,
        is_24h_icu_duty=shift.is_24h_icu_duty,
        is_fatigue_risk=shift.is_fatigue_risk,
        fatigue_level=fatigue_level,
        continuous_hours_at_end=shift.continuous_hours_at_end,
        fatigue_risk_hours=shift.fatigue_risk_hours,
        notes=shift.notes,
        warnings=warnings or [],
    )


def _raise_http_from_validation_error(exc: ScheduleValidationError) -> None:
    """Maps domain validation exceptions to semantic HTTP 409 / 422 status codes."""
    if isinstance(exc, TimeOverlapError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": exc.code,
                "message": exc.message,
                "doctor_id": exc.doctor_id,
                "conflicting_shift_id": exc.conflicting_shift_id,
                "conflict_start_ts": exc.conflict_start_ts,
                "conflict_end_ts": exc.conflict_end_ts,
            },
        ) from exc
    if isinstance(exc, (HospitalMismatchError, TemporalContainmentError)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


# =====================================================================
# 2. SHIFT & TASK CRUD ENDPOINTS
# =====================================================================

@router.get(
    "",
    response_model=List[ShiftResponse],
    status_code=status.HTTP_200_OK,
    summary="List Shifts & Operational Tasks Across All Hospitals",
)
async def list_shifts(
    hospital_id: Optional[int] = Query(default=None, description="Filter by hospital ID"),
    doctor_id: Optional[int] = Query(default=None, description="Filter by assigned doctor ID"),
    shift_type: Optional[ShiftType] = Query(default=None, description="BASE_SHIFT or OPERATIONAL_TASK"),
    parent_shift_id: Optional[int] = Query(default=None, description="Fetch child tasks of a Base Shift"),
    start_ts: Optional[int] = Query(default=None, description="Overlap window start (UTC Unix ts)"),
    end_ts: Optional[int] = Query(default=None, description="Overlap window end (UTC Unix ts)"),
    include_cancelled: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> List[ShiftResponse]:
    """
    Returns scheduled shifts and child operational tasks.
    Doctors, Senior Residents, and Head of Dept all have global read visibility across hospitals.
    """
    conditions = []
    if not include_cancelled:
        conditions.append(Shift.status != ShiftStatus.CANCELLED)
    if hospital_id is not None:
        conditions.append(Shift.hospital_id == hospital_id)
    if doctor_id is not None:
        conditions.append(Shift.doctor_id == doctor_id)
    if shift_type is not None:
        conditions.append(Shift.shift_type == shift_type)
    if parent_shift_id is not None:
        conditions.append(Shift.parent_shift_id == parent_shift_id)
    if start_ts is not None and end_ts is not None:
        # Mathematical interval overlap filter: StartA < EndB AND EndA > StartB
        conditions.append(Shift.start_ts < end_ts)
        conditions.append(Shift.end_ts > start_ts)
    elif start_ts is not None:
        conditions.append(Shift.end_ts > start_ts)
    elif end_ts is not None:
        conditions.append(Shift.start_ts < end_ts)

    stmt = select(Shift).where(and_(*conditions)).order_by(Shift.start_ts, Shift.id)
    result = await session.execute(stmt)
    shifts = result.scalars().all()
    return [_to_shift_response(s) for s in shifts]


@router.post(
    "",
    response_model=ShiftResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Parent Base Shift or Child Operational Task",
)
async def create_shift(
    payload: ShiftCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ShiftResponse:
    """
    Creates a new Base Shift or Operational Task after verifying:
    1. RBAC hospital write access (`SENIOR_RESIDENT` for `assigned_hospital_id`, or `HEAD_OF_DEPT`).
    2. `ShiftValidationService.validate_shift_before_save()` (overlap check, parent-child hospital
       match, temporal containment, and 32h fatigue calculation).
    """
    # Enforce Senior Resident hospital scope or Head of Dept role
    verify_hospital_access(current_user, payload.hospital_id)

    try:
        async with atomic_transaction(session):
            val_result = await ShiftValidationService.validate_shift_before_save(
                session=session,
                doctor_id=payload.doctor_id,
                hospital_id=payload.hospital_id,
                workplace_id=payload.workplace_id,
                start_ts=payload.start_ts,
                end_ts=payload.end_ts,
                shift_type=payload.shift_type,
                parent_shift_id=payload.parent_shift_id,
            )

            now_ts = utc_now_ts()
            new_shift = Shift(
                shift_type=payload.shift_type,
                parent_shift_id=payload.parent_shift_id,
                doctor_id=payload.doctor_id,
                hospital_id=payload.hospital_id,
                workplace_id=payload.workplace_id,
                start_ts=payload.start_ts,
                end_ts=payload.end_ts,
                status=ShiftStatus.SCHEDULED,
                is_24h_icu_duty=payload.is_24h_icu_duty,
                is_fatigue_risk=val_result.fatigue.is_fatigue_risk,
                continuous_hours_at_end=val_result.fatigue.continuous_hours_at_end,
                fatigue_risk_hours=val_result.fatigue.fatigue_risk_hours,
                notes=payload.notes,
                created_by_id=current_user.id,
                created_at=now_ts,
                updated_at=now_ts,
            )
            session.add(new_shift)
            await session.flush()

            audit_action = (
                AuditAction.FATIGUE_OVERRIDE
                if val_result.fatigue.is_fatigue_risk
                else AuditAction.CREATE
            )
            session.add(
                AuditLog(
                    actor_id=current_user.id,
                    actor_role=current_user.role,
                    hospital_id=payload.hospital_id,
                    entity_type="Shift",
                    entity_id=new_shift.id or 0,
                    action=audit_action,
                    previous_state=None,
                    new_state={
                        "shift_type": new_shift.shift_type.value,
                        "parent_shift_id": new_shift.parent_shift_id,
                        "doctor_id": new_shift.doctor_id,
                        "hospital_id": new_shift.hospital_id,
                        "workplace_id": new_shift.workplace_id,
                        "start_ts": new_shift.start_ts,
                        "end_ts": new_shift.end_ts,
                        "fatigue_level": val_result.fatigue.fatigue_level.value,
                        "continuous_hours_at_end": new_shift.continuous_hours_at_end,
                    },
                    description=(
                        f"Created {new_shift.shift_type.value} #{new_shift.id} for doctor "
                        f"#{new_shift.doctor_id} ({val_result.fatigue.fatigue_level.value})."
                    ),
                    created_at=now_ts,
                )
            )
            await session.flush()

            return _to_shift_response(new_shift, warnings=val_result.warnings)

    except ScheduleValidationError as exc:
        _raise_http_from_validation_error(exc)


@router.patch(
    "/{shift_id}",
    response_model=ShiftResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a Base Shift or Operational Task",
)
async def update_shift(
    shift_id: int,
    payload: ShiftUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ShiftResponse:
    """
    Updates an existing Shift or Operational Task, re-running the Validation Service
    and cascading `doctor_id` changes to child `OPERATIONAL_TASK` rows if a parent shift's
    assigned doctor is changed.
    """
    try:
        async with atomic_transaction(session):
            shift = await session.get(Shift, shift_id)
            if shift is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Shift #{shift_id} not found.",
                )

            # Verify RBAC access for the shift's hospital
            verify_hospital_access(current_user, shift.hospital_id)

            before_snapshot = {
                "doctor_id": shift.doctor_id,
                "workplace_id": shift.workplace_id,
                "start_ts": shift.start_ts,
                "end_ts": shift.end_ts,
                "status": shift.status.value,
            }

            new_doctor_id = payload.doctor_id if payload.doctor_id is not None else shift.doctor_id
            new_workplace_id = (
                payload.workplace_id if payload.workplace_id is not None else shift.workplace_id
            )
            new_start_ts = payload.start_ts if payload.start_ts is not None else shift.start_ts
            new_end_ts = payload.end_ts if payload.end_ts is not None else shift.end_ts

            val_result = await ShiftValidationService.validate_shift_before_save(
                session=session,
                doctor_id=new_doctor_id,
                hospital_id=shift.hospital_id,
                workplace_id=new_workplace_id,
                start_ts=new_start_ts,
                end_ts=new_end_ts,
                shift_type=shift.shift_type,
                parent_shift_id=shift.parent_shift_id,
                exclude_shift_id=shift.id,
            )

            now_ts = utc_now_ts()
            shift.doctor_id = new_doctor_id
            shift.workplace_id = new_workplace_id
            shift.start_ts = new_start_ts
            shift.end_ts = new_end_ts
            if payload.is_24h_icu_duty is not None:
                shift.is_24h_icu_duty = payload.is_24h_icu_duty
            if payload.status is not None:
                shift.status = payload.status
            if payload.notes is not None:
                shift.notes = payload.notes

            shift.is_fatigue_risk = val_result.fatigue.is_fatigue_risk
            shift.continuous_hours_at_end = val_result.fatigue.continuous_hours_at_end
            shift.fatigue_risk_hours = val_result.fatigue.fatigue_risk_hours
            shift.updated_at = now_ts
            session.add(shift)

            # If a Parent BASE_SHIFT changed doctor_id, keep child tasks synchronized
            if shift.shift_type == ShiftType.BASE_SHIFT and payload.doctor_id is not None:
                child_stmt = select(Shift).where(
                    and_(
                        Shift.parent_shift_id == shift.id,
                        Shift.status != ShiftStatus.CANCELLED,
                    )
                )
                children = (await session.execute(child_stmt)).scalars().all()
                for child in children:
                    if child.start_ts < new_start_ts or child.end_ts > new_end_ts:
                        raise TemporalContainmentError(
                            f"Updated parent window [{new_start_ts}, {new_end_ts}) would orphan "
                            f"child Operational Task #{child.id} [{child.start_ts}, {child.end_ts})."
                        )
                    child.doctor_id = new_doctor_id
                    child.updated_at = now_ts
                    session.add(child)

            session.add(
                AuditLog(
                    actor_id=current_user.id,
                    actor_role=current_user.role,
                    hospital_id=shift.hospital_id,
                    entity_type="Shift",
                    entity_id=shift.id or shift_id,
                    action=AuditAction.UPDATE,
                    previous_state=before_snapshot,
                    new_state={
                        "doctor_id": shift.doctor_id,
                        "workplace_id": shift.workplace_id,
                        "start_ts": shift.start_ts,
                        "end_ts": shift.end_ts,
                        "status": shift.status.value,
                        "is_fatigue_risk": shift.is_fatigue_risk,
                    },
                    description=f"Updated Shift #{shift_id} by {current_user.full_name}.",
                    created_at=now_ts,
                )
            )
            await session.flush()

            return _to_shift_response(shift, warnings=val_result.warnings)

    except ScheduleValidationError as exc:
        _raise_http_from_validation_error(exc)


@router.delete(
    "/{shift_id}",
    status_code=status.HTTP_200_OK,
    summary="Cancel a Shift (and Cascade to Child Operational Tasks)",
)
async def cancel_shift(
    shift_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Soft-cancels a Shift (`status = CANCELLED`) to preserve historical audit integrity,
    cascading cancellation to any linked child `OPERATIONAL_TASK` rows.
    """
    async with atomic_transaction(session):
        shift = await session.get(Shift, shift_id)
        if shift is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Shift #{shift_id} not found.",
            )

        verify_hospital_access(current_user, shift.hospital_id)

        now_ts = utc_now_ts()
        prev_status = shift.status.value
        shift.status = ShiftStatus.CANCELLED
        shift.updated_at = now_ts
        session.add(shift)

        cascaded_child_ids: List[int] = []
        if shift.shift_type == ShiftType.BASE_SHIFT:
            child_stmt = select(Shift).where(
                and_(
                    Shift.parent_shift_id == shift.id,
                    Shift.status != ShiftStatus.CANCELLED,
                )
            )
            children = (await session.execute(child_stmt)).scalars().all()
            for child in children:
                child.status = ShiftStatus.CANCELLED
                child.updated_at = now_ts
                session.add(child)
                if child.id is not None:
                    cascaded_child_ids.append(child.id)

        session.add(
            AuditLog(
                actor_id=current_user.id,
                actor_role=current_user.role,
                hospital_id=shift.hospital_id,
                entity_type="Shift",
                entity_id=shift.id or shift_id,
                action=AuditAction.DELETE,
                previous_state={"status": prev_status},
                new_state={
                    "status": ShiftStatus.CANCELLED.value,
                    "cascaded_child_ids": cascaded_child_ids,
                },
                description=f"Cancelled Shift #{shift_id} by {current_user.full_name}.",
                created_at=now_ts,
            )
        )
        await session.flush()

        return {
            "id": shift_id,
            "status": ShiftStatus.CANCELLED.value,
            "cascaded_child_ids": cascaded_child_ids,
        }
