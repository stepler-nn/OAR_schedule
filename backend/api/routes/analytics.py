"""
api/routes/analytics.py — ICU Coverage Gap Alerts & 32-Hour Fatigue Analytics Endpoints.

Endpoints:
- `GET /api/v1/analytics/coverage-gaps`:
  Runs `ShiftValidationService.inspect_icu_coverage_gaps()` over a requested UTC window
  and flags any hospital lacking an active 24h ICU duty doctor on any day.
- `GET /api/v1/analytics/fatigue-metrics`:
  Aggregates >24h (`FATIGUE_RISK_HIGH`, e.g., 32h continuous shifts) exposure per doctor
  and per hospital for administrative review and payroll/time-sheet exports.
"""

from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import require_role, verify_hospital_access
from database import get_session
from models import Shift, ShiftStatus, ShiftType, User, UserRole
from services.validation import ShiftValidationService

router = APIRouter(prefix="/analytics", tags=["ICU Coverage & Fatigue Analytics"])


# =====================================================================
# 1. RESPONSE SCHEMAS
# =====================================================================

class ICUCoverageGapResponse(BaseModel):
    hospital_id: int
    hospital_code: str
    hospital_name: str
    day_iso: str
    window_start_ts: int
    window_end_ts: int
    covered_hours: float
    missing_hours: float
    unassigned_intervals: List[Tuple[int, int]]


class CoverageReportResponse(BaseModel):
    range_start_ts: int
    range_end_ts: int
    total_gaps_found: int
    all_hospitals_fully_covered: bool
    gaps: List[ICUCoverageGapResponse]


class DoctorFatigueSummary(BaseModel):
    doctor_id: int
    full_name: str
    email: str
    total_scheduled_hours: float
    fatigue_risk_shifts_count: int
    total_fatigue_risk_hours: float
    max_continuous_hours_observed: float


class FatigueAnalyticsResponse(BaseModel):
    range_start_ts: int
    range_end_ts: int
    hospital_id_filter: Optional[int]
    total_fatigue_flagged_shifts: int
    total_fatigue_excess_hours: float
    doctors: List[DoctorFatigueSummary]


# =====================================================================
# 2. ANALYTICS ENDPOINTS
# =====================================================================

@router.get(
    "/coverage-gaps",
    response_model=CoverageReportResponse,
    status_code=status.HTTP_200_OK,
    summary="Inspect 24h ICU Duty Coverage Gaps Across Hospitals",
)
async def get_icu_coverage_gaps(
    range_start_ts: int = Query(..., description="Window start UTC Unix timestamp"),
    range_end_ts: int = Query(..., description="Window end UTC Unix timestamp"),
    hospital_id: Optional[int] = Query(
        default=None,
        description="Optional single hospital filter (Senior Residents default to their hospital)",
    ),
    current_user: User = Depends(
        require_role([UserRole.SENIOR_RESIDENT, UserRole.HEAD_OF_DEPT])
    ),
    session: AsyncSession = Depends(get_session),
) -> CoverageReportResponse:
    """
    Executes the Hospital ICU Coverage Engine over `[range_start_ts, range_end_ts)`
    and returns every day/hospital missing a 24h ICU duty physician.
    """
    target_hospitals: Optional[List[int]] = None
    if hospital_id is not None:
        verify_hospital_access(current_user, hospital_id)
        target_hospitals = [hospital_id]
    elif current_user.role == UserRole.SENIOR_RESIDENT and current_user.assigned_hospital_id:
        target_hospitals = [current_user.assigned_hospital_id]

    gaps = await ShiftValidationService.inspect_icu_coverage_gaps(
        session=session,
        range_start_ts=range_start_ts,
        range_end_ts=range_end_ts,
        hospital_ids=target_hospitals,
    )

    gap_items = [
        ICUCoverageGapResponse(
            hospital_id=g.hospital_id,
            hospital_code=g.hospital_code,
            hospital_name=g.hospital_name,
            day_iso=g.day_iso,
            window_start_ts=g.window_start_ts,
            window_end_ts=g.window_end_ts,
            covered_hours=g.covered_hours,
            missing_hours=g.missing_hours,
            unassigned_intervals=g.unassigned_intervals,
        )
        for g in gaps
    ]

    return CoverageReportResponse(
        range_start_ts=range_start_ts,
        range_end_ts=range_end_ts,
        total_gaps_found=len(gap_items),
        all_hospitals_fully_covered=len(gap_items) == 0,
        gaps=gap_items,
    )


@router.get(
    "/fatigue-metrics",
    response_model=FatigueAnalyticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Aggregate >24h Continuous Shift (32h) Fatigue Risk Metrics",
)
async def get_fatigue_metrics(
    range_start_ts: int = Query(..., description="Window start UTC Unix timestamp"),
    range_end_ts: int = Query(..., description="Window end UTC Unix timestamp"),
    hospital_id: Optional[int] = Query(default=None),
    current_user: User = Depends(
        require_role([UserRole.SENIOR_RESIDENT, UserRole.HEAD_OF_DEPT])
    ),
    session: AsyncSession = Depends(get_session),
) -> FatigueAnalyticsResponse:
    """
    Computes per-doctor and department-wide fatigue telemetry for shifts overlapping
    `[range_start_ts, range_end_ts)`. Highlights doctors working 32-hour continuous stretches.
    """
    effective_hospital_id = hospital_id
    if current_user.role == UserRole.SENIOR_RESIDENT:
        effective_hospital_id = hospital_id or current_user.assigned_hospital_id
        if effective_hospital_id is not None:
            verify_hospital_access(current_user, effective_hospital_id)

    conditions = [
        Shift.shift_type == ShiftType.BASE_SHIFT,
        Shift.status != ShiftStatus.CANCELLED,
        Shift.start_ts < range_end_ts,
        Shift.end_ts > range_start_ts,
    ]
    if effective_hospital_id is not None:
        conditions.append(Shift.hospital_id == effective_hospital_id)

    stmt = (
        select(Shift, User)
        .join(User, User.id == Shift.doctor_id)
        .where(and_(*conditions))
        .order_by(Shift.doctor_id, Shift.start_ts)
    )
    rows = (await session.execute(stmt)).all()

    by_doctor: Dict[int, DoctorFatigueSummary] = {}
    total_flagged_shifts = 0
    total_excess_hours = 0.0

    for shift, doctor in rows:
        doc_id = doctor.id or shift.doctor_id
        if doc_id not in by_doctor:
            by_doctor[doc_id] = DoctorFatigueSummary(
                doctor_id=doc_id,
                full_name=doctor.full_name,
                email=doctor.email,
                total_scheduled_hours=0.0,
                fatigue_risk_shifts_count=0,
                total_fatigue_risk_hours=0.0,
                max_continuous_hours_observed=0.0,
            )

        summary = by_doctor[doc_id]
        summary.total_scheduled_hours = round(
            summary.total_scheduled_hours + shift.duration_hours, 2
        )
        summary.max_continuous_hours_observed = max(
            summary.max_continuous_hours_observed,
            shift.continuous_hours_at_end,
            shift.duration_hours,
        )

        if shift.is_fatigue_risk:
            summary.fatigue_risk_shifts_count += 1
            summary.total_fatigue_risk_hours = round(
                summary.total_fatigue_risk_hours + shift.fatigue_risk_hours, 2
            )
            total_flagged_shifts += 1
            total_excess_hours = round(total_excess_hours + shift.fatigue_risk_hours, 2)

    sorted_doctors = sorted(
        by_doctor.values(),
        key=lambda d: (d.total_fatigue_risk_hours, d.total_scheduled_hours),
        reverse=True,
    )

    return FatigueAnalyticsResponse(
        range_start_ts=range_start_ts,
        range_end_ts=range_end_ts,
        hospital_id_filter=effective_hospital_id,
        total_fatigue_flagged_shifts=total_flagged_shifts,
        total_fatigue_excess_hours=total_excess_hours,
        doctors=sorted_doctors,
    )
