"""
api/routes/timelogs.py — Doctor PWA Check-In / Check-Out & Time Tracking Endpoints.

Domain Responsibilities:
1. `POST /api/v1/timelogs/check-in`:
   - Allows an authenticated physician to check in to their assigned shift via the PWA.
   - Transitions `Shift.status` to `IN_PROGRESS` and records `check_in_ts` + PWA metadata.
2. `POST /api/v1/timelogs/{timelog_id}/check-out`:
   - Records `check_out_ts`, computes `recorded_duration_seconds`, and checks whether actual
     continuous worked duration (including adjacent consecutive shifts) exceeds 24 hours
     (`> 86,400` seconds), flagging `is_fatigue_flagged = True` when applicable.
   - Transitions `Shift.status` to `COMPLETED`.
3. `GET /api/v1/timelogs`:
   - Doctors view their own attendance logs; Senior Residents view logs for their
     `assigned_hospital_id`; Head of Dept views global attendance across all hospitals.
"""

from __future__ import annotations

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
    TimeLog,
    User,
    UserRole,
    utc_now_ts,
)
from services.validation import FATIGUE_THRESHOLD_SECONDS, ShiftValidationService

router = APIRouter(prefix="/timelogs", tags=["PWA Time Tracking (Check-In / Check-Out)"])


# =====================================================================
# 1. REQUEST & RESPONSE SCHEMAS
# =====================================================================

class CheckInRequest(BaseModel):
    """Payload sent by the Vue 3 PWA when a doctor checks in for a shift."""
    shift_id: int
    check_in_ts: Optional[int] = Field(
        default=None,
        description="Optional client UTC timestamp (defaults to server UTC epoch if omitted)",
    )
    pwa_client_metadata: Optional[str] = Field(
        default=None,
        max_length=255,
        description="PWA device/offline sync signature or hospital network beacon",
    )
    notes: Optional[str] = Field(default=None, max_length=500)


class CheckOutRequest(BaseModel):
    """Payload sent by the Vue 3 PWA when a doctor completes and checks out of a shift."""
    check_out_ts: Optional[int] = Field(
        default=None,
        description="Optional client UTC timestamp (defaults to server UTC epoch if omitted)",
    )
    notes: Optional[str] = Field(default=None, max_length=500)


class TimeLogResponse(BaseModel):
    """Serialized TimeLog entry."""
    id: int
    shift_id: int
    doctor_id: int
    check_in_ts: int
    check_out_ts: Optional[int]
    recorded_duration_seconds: Optional[int]
    recorded_duration_hours: Optional[float]
    is_fatigue_flagged: bool
    pwa_client_metadata: Optional[str]
    notes: Optional[str]
    created_at: int


def _to_timelog_response(log: TimeLog) -> TimeLogResponse:
    hours = (
        round(log.recorded_duration_seconds / 3600.0, 2)
        if log.recorded_duration_seconds is not None
        else None
    )
    return TimeLogResponse(
        id=log.id or 0,
        shift_id=log.shift_id,
        doctor_id=log.doctor_id,
        check_in_ts=log.check_in_ts,
        check_out_ts=log.check_out_ts,
        recorded_duration_seconds=log.recorded_duration_seconds,
        recorded_duration_hours=hours,
        is_fatigue_flagged=log.is_fatigue_flagged,
        pwa_client_metadata=log.pwa_client_metadata,
        notes=log.notes,
        created_at=log.created_at,
    )


# =====================================================================
# 2. PWA CHECK-IN / CHECK-OUT ENDPOINTS
# =====================================================================

@router.post(
    "/check-in",
    response_model=TimeLogResponse,
    status_code=status.HTTP_201_CREATED,
    summary="PWA Doctor Check-In for Assigned Shift",
)
async def pwa_check_in(
    payload: CheckInRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TimeLogResponse:
    """Records a doctor's PWA check-in and marks the shift `IN_PROGRESS`."""
    async with atomic_transaction(session):
        shift = await session.get(Shift, payload.shift_id)
        if shift is None or shift.status == ShiftStatus.CANCELLED:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Shift #{payload.shift_id} not found or cancelled.",
            )

        if shift.doctor_id != current_user.id and current_user.role != UserRole.HEAD_OF_DEPT:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only check in to shifts assigned to you.",
            )

        # Ensure there isn't already an open check-in for this shift
        existing_stmt = select(TimeLog).where(
            and_(
                TimeLog.shift_id == shift.id,
                TimeLog.doctor_id == shift.doctor_id,
                TimeLog.check_out_ts.is_(None),
            )
        )
        open_log = (await session.execute(existing_stmt)).scalars().first()
        if open_log is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An active check-in (TimeLog #{open_log.id}) already exists for Shift #{shift.id}.",
            )

        now_ts = utc_now_ts()
        effective_checkin_ts = payload.check_in_ts if payload.check_in_ts is not None else now_ts

        timelog = TimeLog(
            shift_id=shift.id or payload.shift_id,
            doctor_id=shift.doctor_id,
            check_in_ts=effective_checkin_ts,
            check_out_ts=None,
            recorded_duration_seconds=None,
            is_fatigue_flagged=shift.is_fatigue_risk,
            pwa_client_metadata=payload.pwa_client_metadata,
            notes=payload.notes,
            created_at=now_ts,
        )
        session.add(timelog)

        shift.status = ShiftStatus.IN_PROGRESS
        shift.updated_at = now_ts
        session.add(shift)
        await session.flush()

        session.add(
            AuditLog(
                actor_id=current_user.id,
                actor_role=current_user.role,
                hospital_id=shift.hospital_id,
                entity_type="TimeLog",
                entity_id=timelog.id or 0,
                action=AuditAction.CHECK_IN,
                previous_state={"shift_status": ShiftStatus.SCHEDULED.value},
                new_state={
                    "shift_status": ShiftStatus.IN_PROGRESS.value,
                    "check_in_ts": effective_checkin_ts,
                },
                description=f"PWA Check-In for Shift #{shift.id} by Doctor #{shift.doctor_id}.",
                created_at=now_ts,
            )
        )
        await session.flush()

        return _to_timelog_response(timelog)


@router.post(
    "/{timelog_id}/check-out",
    response_model=TimeLogResponse,
    status_code=status.HTTP_200_OK,
    summary="PWA Doctor Check-Out & Actual Fatigue Verification",
)
async def pwa_check_out(
    timelog_id: int,
    payload: CheckOutRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TimeLogResponse:
    """
    Completes an open `TimeLog`, calculates `recorded_duration_seconds`, evaluates
    stitched continuous hours via `ShiftValidationService.calculate_doctor_fatigue`,
    and flags `is_fatigue_flagged = True` if continuous work exceeds 24 hours.
    """
    async with atomic_transaction(session):
        timelog = await session.get(TimeLog, timelog_id)
        if timelog is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"TimeLog #{timelog_id} not found.",
            )

        if timelog.doctor_id != current_user.id and current_user.role != UserRole.HEAD_OF_DEPT:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only check out of your own active TimeLog.",
            )

        if timelog.check_out_ts is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"TimeLog #{timelog_id} has already been checked out.",
            )

        now_ts = utc_now_ts()
        effective_checkout_ts = payload.check_out_ts if payload.check_out_ts is not None else now_ts

        if effective_checkout_ts < timelog.check_in_ts:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="check_out_ts cannot be earlier than check_in_ts.",
            )

        duration_sec = effective_checkout_ts - timelog.check_in_ts
        timelog.check_out_ts = effective_checkout_ts
        timelog.recorded_duration_seconds = duration_sec
        if payload.notes:
            timelog.notes = payload.notes

        shift = await session.get(Shift, timelog.shift_id)
        fatigue_assessment = await ShiftValidationService.calculate_doctor_fatigue(
            session=session,
            doctor_id=timelog.doctor_id,
            proposed_start_ts=timelog.check_in_ts,
            proposed_end_ts=effective_checkout_ts,
            exclude_shift_id=timelog.shift_id,
        )

        timelog.is_fatigue_flagged = (
            duration_sec > FATIGUE_THRESHOLD_SECONDS or fatigue_assessment.is_fatigue_risk
        )
        session.add(timelog)

        if shift is not None:
            shift.status = ShiftStatus.COMPLETED
            shift.updated_at = now_ts
            session.add(shift)

        session.add(
            AuditLog(
                actor_id=current_user.id,
                actor_role=current_user.role,
                hospital_id=shift.hospital_id if shift else None,
                entity_type="TimeLog",
                entity_id=timelog.id or timelog_id,
                action=AuditAction.CHECK_OUT,
                previous_state={"check_out_ts": None},
                new_state={
                    "check_out_ts": effective_checkout_ts,
                    "recorded_duration_seconds": duration_sec,
                    "is_fatigue_flagged": timelog.is_fatigue_flagged,
                },
                description=(
                    f"PWA Check-Out for TimeLog #{timelog_id} "
                    f"({round(duration_sec / 3600.0, 2)}h worked)."
                ),
                created_at=now_ts,
            )
        )
        await session.flush()

        return _to_timelog_response(timelog)


@router.get(
    "",
    response_model=List[TimeLogResponse],
    status_code=status.HTTP_200_OK,
    summary="List TimeLogs (Scoped by Role)",
)
async def list_timelogs(
    doctor_id: Optional[int] = Query(default=None),
    hospital_id: Optional[int] = Query(default=None),
    fatigue_only: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> List[TimeLogResponse]:
    """
    Returns attendance logs:
    - `DOCTOR`: Restricted to their own `TimeLog` entries.
    - `SENIOR_RESIDENT`: Restricted to shifts in their `assigned_hospital_id`.
    - `HEAD_OF_DEPT`: Global access across all hospitals and physicians.
    """
    stmt = select(TimeLog).join(Shift, Shift.id == TimeLog.shift_id)
    conditions = []

    if current_user.role == UserRole.DOCTOR:
        conditions.append(TimeLog.doctor_id == current_user.id)
    elif current_user.role == UserRole.SENIOR_RESIDENT:
        target_hosp = hospital_id or current_user.assigned_hospital_id
        if target_hosp is None:
            raise HTTPException(status_code=403, detail="Senior Resident missing assigned_hospital_id.")
        verify_hospital_access(current_user, target_hosp)
        conditions.append(Shift.hospital_id == target_hosp)
    elif hospital_id is not None:
        conditions.append(Shift.hospital_id == hospital_id)

    if doctor_id is not None and current_user.role != UserRole.DOCTOR:
        conditions.append(TimeLog.doctor_id == doctor_id)

    if fatigue_only:
        conditions.append(TimeLog.is_fatigue_flagged == True)  # noqa: E712

    stmt = stmt.where(and_(*conditions)).order_by(TimeLog.check_in_ts.desc())
    result = await session.execute(stmt)
    return [_to_timelog_response(row) for row in result.scalars().all()]
