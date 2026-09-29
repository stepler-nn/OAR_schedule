"""
services/__init__.py — Core Business Logic & Domain Services for Anesthesiology & ICU Scheduling.
"""

from services.fsm import (
    ALLOWED_STATE_TRANSITIONS,
    FSMError,
    InvalidStateTransitionError,
    RBACPermissionDeniedError,
    ShiftSwapFSMService,
    SwapConflictError,
)
from services.validation import (
    FatigueAssessment,
    FatigueLevel,
    HospitalMismatchError,
    ICUCoverageGap,
    ScheduleValidationError,
    ShiftValidationResult,
    ShiftValidationService,
    TemporalContainmentError,
    TimeOverlapError,
)

__all__ = [
    "ALLOWED_STATE_TRANSITIONS",
    "FSMError",
    "FatigueAssessment",
    "FatigueLevel",
    "HospitalMismatchError",
    "ICUCoverageGap",
    "InvalidStateTransitionError",
    "RBACPermissionDeniedError",
    "ScheduleValidationError",
    "ShiftSwapFSMService",
    "ShiftValidationResult",
    "ShiftValidationService",
    "SwapConflictError",
    "TemporalContainmentError",
    "TimeOverlapError",
]
