"""
services/validation.py — Shift Validation, ICU Coverage & 32h Fatigue Calculation Engine.

Core Domain Responsibilities:
1. Time Overlap Check (`StartA < EndB AND EndA > StartB`):
   - Prevents a doctor from being assigned two overlapping primary (`BASE_SHIFT`) shifts
     across any hospital.
   - Prevents overlapping sibling `OPERATIONAL_TASK` assignments within the same parent shift.
   - Explicitly permits an `OPERATIONAL_TASK` (e.g., 08:00-16:00 OR #2) to overlap with its
     own parent `BASE_SHIFT` (e.g., 24h ICU duty at the same hospital).
2. Hospital Consistency & Temporal Containment:
   - Enforces that any child `OPERATIONAL_TASK` belongs to the exact same `hospital_id`
     as its parent 24h `BASE_SHIFT` (`child.hospital_id == parent.hospital_id`).
   - Enforces that the child task's `[start_ts, end_ts]` window is strictly contained within
     the parent shift's `[start_ts, end_ts]` window.
3. Hospital ICU Coverage Engine:
   - Inspects any UTC date/time range across all active hospitals requiring 24h ICU coverage
     and returns structured `ICUCoverageGap` alerts for any hospital/day missing a scheduled
     24h ICU duty doctor.
4. Consecutive Shift Stitching & 32-Hour Fatigue Calculation:
   - Stitches contiguous shifts (where `ShiftA.end_ts == ShiftB.start_ts`, or separated by less
     than `MIN_REST_RESET_SECONDS`) for a doctor.
   - If cumulative continuous work exceeds 24 hours (e.g., 24h ICU duty + 8h daytime OR = 32h),
     it flags the shift with `FatigueLevel.FATIGUE_RISK_HIGH` (`is_fatigue_risk = True`) and
     computes `fatigue_risk_hours` WITHOUT blocking schedule creation.
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

# Threshold above which continuous work is tagged FATIGUE_RISK_HIGH (non-blocking)
FATIGUE_THRESHOLD_HOURS: float = 24.0
FATIGUE_THRESHOLD_SECONDS: int = int(FATIGUE_THRESHOLD_HOURS * SECONDS_PER_HOUR)

# Minimum rest break required to reset a doctor's continuous-work counter (e.g., 2 hours).
# Shifts separated by 0 seconds (back-to-back 24h + 8h = 32h) are stitched into a single chain.
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
    """Raised when a doctor has a conflicting shift (`StartA < EndB AND EndA > StartB`)."""

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
    Raised when a child Operational Task's time interval falls outside its Parent Base Shift's window,
    or when parent/child roles or doctor assignments are inconsistent.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="TEMPORAL_CONTAINMENT_VIOLATION")


# =====================================================================
# 2. DATA TRANSFER & DIAGNOSTIC STRUCTURES
# =====================================================================

@dataclass(frozen=True)
class FatigueAssessment:
    """
    Result of stitching consecutive shifts for a doctor around a proposed shift.
    """
    fatigue_level: FatigueLevel
    is_fatigue_risk: bool
    continuous_hours_at_end: float
    fatigue_risk_hours: float
    chain_start_ts: int
    chain_end_ts: int
    stitched_shift_ids: List[int] = field(default_factory=list)


@dataclass(frozen=True)
class ShiftValidationResult:
    """
    Returned by `ShiftValidationService.validate_shift_before_save()` when all blocking
    constraints pass. Contains the non-blocking fatigue telemetry to persist on `Shift`.
    """
    is_valid: bool
    fatigue: FatigueAssessment
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ICUCoverageGap:
    """
    Represents a 24h ICU coverage deficit at a specific hospital on a specific UTC date.
    """
    hospital_id: int
    hospital_code: str
    hospital_name: str
    day_iso: str               # e.g., "2026-10-01"
    window_start_ts: int       # UTC day start epoch (08:00 or 00:00 anchor)
    window_end_ts: int         # UTC day end epoch (+86,400s)
    covered_hours: float
    missing_hours: float
    unassigned_intervals: List[Tuple[int, int]]


# =====================================================================
# 3. SHIFT VALIDATION & COVERAGE ENGINE SERVICE
# =====================================================================

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
        """
        Executes the full validation pipeline on a proposed Shift or Operational Task:
          1. Positive interval & Workplace-Hospital ownership check.
          2. Parent-Child Hospital Consistency & Temporal Containment check (if OPERATIONAL_TASK).
          3. Mathematical Time Overlap Check (`StartA < EndB AND EndA > StartB`).
          4. Consecutive Shift Stitching & 32-hour Fatigue Calculation (`FATIGUE_RISK_HIGH`).

        Raises:
            ScheduleValidationError (or subclass) if a hard physical/temporal constraint fails.
        Returns:
            ShiftValidationResult containing non-blocking `FatigueAssessment` flags.
        """
        if end_ts <= start_ts:
            raise ScheduleValidationError(
                f"Shift end_ts ({end_ts}) must be strictly greater than start_ts ({start_ts}).",
                code="INVALID_TIME_INTERVAL",
            )

        # 1. Verify workplace belongs to the target hospital
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

        # 2. Validate Parent-Child Hierarchy if this is an Operational Task (or Base Shift)
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

        # 3. Mathematical Time Overlap Check: StartA < EndB AND EndA > StartB
        await cls.check_time_overlap(
            session=session,
            doctor_id=doctor_id,
            start_ts=start_ts,
            end_ts=end_ts,
            shift_type=shift_type,
            parent_shift_id=parent_shift_id,
            exclude_shift_id=exclude_shift_id,
        )

        # 4. Fatigue Calculation (stitched across consecutive Base Shifts)
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
                f"({fatigue.fatigue_risk_hours:.1f}h above the 24h threshold). "
                "Allowed by scheduling policy; tagged for administrative review."
            )

        return ShiftValidationResult(
            is_valid=True,
            fatigue=fatigue,
            warnings=warnings,
        )

    @classmethod
    async def _validate_child_task_against_parent(
        cls,
        session: AsyncSession,
        *,
        doctor_id: int,
        child_hospital_id: int,
        child_start_ts: int,
        child_end_ts: int,
        parent_shift_id: int,
    ) -> Shift:
        """
        Ensures a child Operational Task (e.g., 08:00-16:00 OR #2) satisfies:
          - Parent shift exists, is active, and is a `BASE_SHIFT`.
          - Physical Restriction: `child_hospital_id == parent.hospital_id`.
          - Doctor Consistency: `doctor_id == parent.doctor_id`.
          - Temporal Containment: `child_start_ts >= parent.start_ts` and `child_end_ts <= parent.end_ts`.
        """
        parent = await session.get(Shift, parent_shift_id)
        if parent is None or parent.status == ShiftStatus.CANCELLED:
            raise TemporalContainmentError(
                f"Parent Base Shift #{parent_shift_id} does not exist or is cancelled."
            )

        if parent.shift_type != ShiftType.BASE_SHIFT:
            raise TemporalContainmentError(
                f"Shift #{parent_shift_id} is an {parent.shift_type.value}; "
                "child tasks can only attach to a BASE_SHIFT."
            )

        # Physical hospital restriction: cannot assign OR task in Hospital B if Parent Shift is in Hospital A
        if child_hospital_id != parent.hospital_id:
            raise HospitalMismatchError(
                child_hospital_id=child_hospital_id,
                parent_hospital_id=parent.hospital_id,
                parent_shift_id=parent_shift_id,
            )

        if doctor_id != parent.doctor_id:
            raise TemporalContainmentError(
                f"Doctor mismatch: Child task doctor_id ({doctor_id}) must match "
                f"Parent Base Shift doctor_id ({parent.doctor_id})."
            )

        if child_start_ts < parent.start_ts or child_end_ts > parent.end_ts:
            raise TemporalContainmentError(
                f"Temporal containment violation: Child task [{child_start_ts}, {child_end_ts}) "
                f"falls outside Parent Base Shift #{parent_shift_id} window "
                f"[{parent.start_ts}, {parent.end_ts})."
            )

        return parent

    @classmethod
    async def check_time_overlap(
        cls,
        session: AsyncSession,
        *,
        doctor_id: int,
        start_ts: int,
        end_ts: int,
        shift_type: ShiftType = ShiftType.BASE_SHIFT,
        parent_shift_id: Optional[int] = None,
        exclude_shift_id: Optional[int] = None,
    ) -> None:
        """
        Mathematical interval overlap check using indexed UTC Unix timestamps:
            StartA < EndB AND EndA > StartB

        Rules:
        - If scheduling a `BASE_SHIFT`: cannot overlap with any other active `BASE_SHIFT`
          for the same doctor. Note that a back-to-back shift where `EndA == StartB`
          (e.g., 24h ICU ending at 08:00 and 8h OR starting at 08:00) evaluates
          `StartA < EndB AND EndA > StartB` as `False` and is therefore allowed!
        - If scheduling an `OPERATIONAL_TASK`: it naturally overlaps with its own
          `parent_shift_id`, so we exclude `Shift.id == parent_shift_id` and check for
          conflicts against any other `OPERATIONAL_TASK` or unrelated `BASE_SHIFT`.
        """
        conditions = [
            Shift.doctor_id == doctor_id,
            Shift.status != ShiftStatus.CANCELLED,
            Shift.start_ts < end_ts,  # StartA < EndB
            Shift.end_ts > start_ts,  # EndA > StartB
        ]

        if exclude_shift_id is not None:
            conditions.append(Shift.id != exclude_shift_id)

        if shift_type == ShiftType.BASE_SHIFT:
            # Base shifts conflict with any other Base Shift for the same doctor
            conditions.append(Shift.shift_type == ShiftType.BASE_SHIFT)
        else:
            # Operational tasks are allowed to sit inside their own parent Base Shift,
            # but must not overlap with sibling Operational Tasks or unrelated Base Shifts.
            if parent_shift_id is not None:
                conditions.append(Shift.id != parent_shift_id)

        stmt = select(Shift).where(and_(*conditions)).order_by(Shift.start_ts).limit(1)
        result = await session.execute(stmt)
        conflicting_shift = result.scalars().first()

        if conflicting_shift is not None:
            raise TimeOverlapError(
                doctor_id=doctor_id,
                conflicting_shift_id=conflicting_shift.id,
                conflict_start_ts=conflicting_shift.start_ts,
                conflict_end_ts=conflicting_shift.end_ts,
            )

    @classmethod
    async def calculate_doctor_fatigue(
        cls,
        session: AsyncSession,
        *,
        doctor_id: int,
        proposed_start_ts: int,
        proposed_end_ts: int,
        shift_type: ShiftType = ShiftType.BASE_SHIFT,
        exclude_shift_id: Optional[int] = None,
    ) -> FatigueAssessment:
        """
        Stitches consecutive or near-contiguous `BASE_SHIFT` assignments for `doctor_id`
        around `[proposed_start_ts, proposed_end_ts]` to compute total continuous work duration.

        Domain Rule:
        - Doctors frequently work a 24h ICU shift followed immediately by an 8h daytime OR shift
          (32 hours of continuous work).
        - If total continuous work exceeds 24.0 hours, this method returns
          `fatigue_level = FatigueLevel.FATIGUE_RISK_HIGH`, `is_fatigue_risk = True`, and
          `fatigue_risk_hours = total_hours - 24.0` WITHOUT raising an exception.
        """
        # Child operational tasks occur *within* a parent Base Shift and do not add extra hours
        # beyond the parent Base Shift's envelope.
        if shift_type == ShiftType.OPERATIONAL_TASK:
            duration_h = round((proposed_end_ts - proposed_start_ts) / SECONDS_PER_HOUR, 2)
            return FatigueAssessment(
                fatigue_level=FatigueLevel.NOMINAL,
                is_fatigue_risk=False,
                continuous_hours_at_end=duration_h,
                fatigue_risk_hours=0.0,
                chain_start_ts=proposed_start_ts,
                chain_end_ts=proposed_end_ts,
                stitched_shift_ids=[],
            )

        # Look back and forward up to 72 hours around the proposed shift to stitch any chain
        lookaround_seconds = 72 * SECONDS_PER_HOUR
        window_min = proposed_start_ts - lookaround_seconds
        window_max = proposed_end_ts + lookaround_seconds

        conditions = [
            Shift.doctor_id == doctor_id,
            Shift.shift_type == ShiftType.BASE_SHIFT,
            Shift.status != ShiftStatus.CANCELLED,
            Shift.end_ts >= window_min,
            Shift.start_ts <= window_max,
        ]
        if exclude_shift_id is not None:
            conditions.append(Shift.id != exclude_shift_id)

        stmt = select(Shift).where(and_(*conditions)).order_by(Shift.start_ts)
        result = await session.execute(stmt)
        neighbor_shifts: Sequence[Shift] = result.scalars().all()

        # Build a list of intervals: (start_ts, end_ts, shift_id_or_none)
        intervals: List[Tuple[int, int, Optional[int]]] = [
            (s.start_ts, s.end_ts, s.id) for s in neighbor_shifts
        ]
        intervals.append((proposed_start_ts, proposed_end_ts, None))
        intervals.sort(key=lambda item: (item[0], item[1]))

        # Stitch contiguous intervals where gap <= MIN_REST_RESET_SECONDS
        chains: List[Tuple[int, int, List[int]]] = []
        for s_ts, e_ts, s_id in intervals:
            if not chains:
                chains.append((s_ts, e_ts, [s_id] if s_id is not None else []))
                continue

            prev_start, prev_end, prev_ids = chains[-1]
            # If this shift touches or starts within MIN_REST_RESET_SECONDS of prev_end, stitch them
            if s_ts - prev_end <= MIN_REST_RESET_SECONDS:
                merged_end = max(prev_end, e_ts)
                if s_id is not None:
                    prev_ids.append(s_id)
                chains[-1] = (prev_start, merged_end, prev_ids)
            else:
                chains.append((s_ts, e_ts, [s_id] if s_id is not None else []))

        # Locate the stitched chain that contains the proposed shift
        target_chain = (proposed_start_ts, proposed_end_ts, [])
        for c_start, c_end, c_ids in chains:
            if c_start <= proposed_start_ts and c_end >= proposed_end_ts:
                target_chain = (c_start, c_end, c_ids)
                break

        chain_start_ts, chain_end_ts, stitched_ids = target_chain
        # Continuous hours accumulated at the end of the stitched chain (or at proposed_end_ts)
        continuous_seconds = chain_end_ts - chain_start_ts
        continuous_hours = round(continuous_seconds / SECONDS_PER_HOUR, 2)

        if continuous_hours > FATIGUE_THRESHOLD_HOURS:
            excess_hours = round(continuous_hours - FATIGUE_THRESHOLD_HOURS, 2)
            return FatigueAssessment(
                fatigue_level=FatigueLevel.FATIGUE_RISK_HIGH,
                is_fatigue_risk=True,
                continuous_hours_at_end=continuous_hours,
                fatigue_risk_hours=excess_hours,
                chain_start_ts=chain_start_ts,
                chain_end_ts=chain_end_ts,
                stitched_shift_ids=stitched_ids,
            )

        return FatigueAssessment(
            fatigue_level=FatigueLevel.NOMINAL,
            is_fatigue_risk=False,
            continuous_hours_at_end=continuous_hours,
            fatigue_risk_hours=0.0,
            chain_start_ts=chain_start_ts,
            chain_end_ts=chain_end_ts,
            stitched_shift_ids=stitched_ids,
        )

    @classmethod
    async def inspect_icu_coverage_gaps(
        cls,
        session: AsyncSession,
        *,
        range_start_ts: int,
        range_end_ts: int,
        hospital_ids: Optional[Sequence[int]] = None,
    ) -> List[ICUCoverageGap]:
        """
        Hospital ICU Coverage Engine:
        Inspects `[range_start_ts, range_end_ts)` across all active hospitals with
        `requires_24h_icu_coverage = True` and identifies any 24-hour slice where a hospital
        lacks an active 24h ICU duty Base Shift (`is_24h_icu_duty == True`).

        Algorithm:
        1. Loads all active hospitals requiring 24h ICU coverage (filtered by `hospital_ids` if given).
        2. Queries all active `BASE_SHIFT` records with `is_24h_icu_duty == True` overlapping
           `[range_start_ts, range_end_ts)`.
        3. Slices the requested range into 24-hour duty windows `[day_start, day_end)` and
           merges overlapping ICU shifts per hospital to detect any uncovered sub-intervals.
        """
        if range_end_ts <= range_start_ts:
            return []

        hosp_conditions = [
            Hospital.is_active == True,  # noqa: E712
            Hospital.requires_24h_icu_coverage == True,  # noqa: E712
        ]
        if hospital_ids:
            hosp_conditions.append(Hospital.id.in_(hospital_ids))

        hosp_result = await session.execute(
            select(Hospital).where(and_(*hosp_conditions)).order_by(Hospital.id)
        )
        hospitals: Sequence[Hospital] = hosp_result.scalars().all()
        if not hospitals:
            return []

        active_hospital_ids = [h.id for h in hospitals if h.id is not None]

        # Fetch all 24h ICU duty Base Shifts overlapping the target window
        shift_stmt = (
            select(Shift)
            .where(
                and_(
                    Shift.hospital_id.in_(active_hospital_ids),
                    Shift.shift_type == ShiftType.BASE_SHIFT,
                    Shift.is_24h_icu_duty == True,  # noqa: E712
                    Shift.status != ShiftStatus.CANCELLED,
                    Shift.start_ts < range_end_ts,
                    Shift.end_ts > range_start_ts,
                )
            )
            .order_by(Shift.hospital_id, Shift.start_ts)
        )
        shift_result = await session.execute(shift_stmt)
        icu_shifts: Sequence[Shift] = shift_result.scalars().all()

        # Group ICU shifts by hospital_id
        shifts_by_hospital: dict[int, List[Shift]] = {h_id: [] for h_id in active_hospital_ids}
        for s in icu_shifts:
            shifts_by_hospital.setdefault(s.hospital_id, []).append(s)

        gaps: List[ICUCoverageGap] = []

        # Step through the range in 24-hour increments (86,400 seconds)
        cursor_ts = range_start_ts
        while cursor_ts < range_end_ts:
            window_end = min(cursor_ts + SECONDS_PER_DAY, range_end_ts)
            window_duration_sec = window_end - cursor_ts
            day_iso = datetime.fromtimestamp(cursor_ts, tz=timezone.utc).strftime("%Y-%m-%d")

            for hosp in hospitals:
                if hosp.id is None:
                    continue
                hosp_shifts = shifts_by_hospital.get(hosp.id, [])

                # Clip shifts to [cursor_ts, window_end)
                clipped: List[Tuple[int, int]] = []
                for s in hosp_shifts:
                    if s.start_ts < window_end and s.end_ts > cursor_ts:
                        clipped.append((max(s.start_ts, cursor_ts), min(s.end_ts, window_end)))

                unassigned = cls._compute_uncovered_subintervals(
                    window_start=cursor_ts,
                    window_end=window_end,
                    covered_intervals=clipped,
                )

                if unassigned:
                    missing_sec = sum(u_end - u_start for u_start, u_end in unassigned)
                    covered_sec = window_duration_sec - missing_sec
                    gaps.append(
                        ICUCoverageGap(
                            hospital_id=hosp.id,
                            hospital_code=hosp.code,
                            hospital_name=hosp.name,
                            day_iso=day_iso,
                            window_start_ts=cursor_ts,
                            window_end_ts=window_end,
                            covered_hours=round(covered_sec / SECONDS_PER_HOUR, 2),
                            missing_hours=round(missing_sec / SECONDS_PER_HOUR, 2),
                            unassigned_intervals=unassigned,
                        )
                    )

            cursor_ts = window_end

        return gaps

    @staticmethod
    def _compute_uncovered_subintervals(
        window_start: int,
        window_end: int,
        covered_intervals: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        """
        Merges overlapping `covered_intervals` inside `[window_start, window_end)`
        and returns the list of uncovered `(gap_start_ts, gap_end_ts)` sub-intervals.
        """
        if not covered_intervals:
            return [(window_start, window_end)]

        sorted_cov = sorted(covered_intervals, key=lambda x: (x[0], x[1]))
        merged: List[Tuple[int, int]] = [sorted_cov[0]]
        for cur_start, cur_end in sorted_cov[1:]:
            last_start, last_end = merged[-1]
            if cur_start <= last_end:
                merged[-1] = (last_start, max(last_end, cur_end))
            else:
                merged.append((cur_start, cur_end))

        gaps: List[Tuple[int, int]] = []
        cursor = window_start
        for cov_start, cov_end in merged:
            if cov_start > cursor:
                gaps.append((cursor, cov_start))
            cursor = max(cursor, cov_end)

        if cursor < window_end:
            gaps.append((cursor, window_end))

        return gaps
